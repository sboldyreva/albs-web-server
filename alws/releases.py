import typing
import urllib.parse

import aiohttp
import jmespath
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from alws import models
from alws.config import settings
from alws.utils.pulp_client import PulpClient


__all__ = [
    'execute_release_plan',
    'get_release_plan',
]


class EmptyReleasePlan(ValueError):
    pass


class MissingRepositories(ValueError):
    pass


class OracleClient:
    def __init__(self):
        self._host = settings.packages_oracle_host
        self._headers = {}
        if settings.packages_oracle_token:
            self._headers['Authorization'] = \
                f'Bearer {settings.packages_oracle_token}'

    def _get_url(self, endpoint: str) -> str:
        return urllib.parse.urljoin(self._host, endpoint)

    async def get(self, endpoint: str, headers: dict = None, params: dict = None):
        req_headers = self._headers.copy()
        if headers:
            req_headers.update(**headers)
        async with aiohttp.ClientSession(headers=req_headers) as session:
            async with session.get(
                    self._get_url(endpoint), params=params) as response:
                json = await response.json(content_type=None)
                response.raise_for_status()
                return json

    async def post(self, endpoint: str, data: typing.Union[dict, list]):
        async with aiohttp.ClientSession(headers=self._headers) as session:
            async with session.post(
                    self._get_url(endpoint), json=data) as response:
                json = await response.json(content_type=None)
                response.raise_for_status()
                return json


async def get_release_plan(source_rpms: typing.List[str], dist_name: str,
                           dist_version: str) -> dict:
    # FIXME: put actual endpoint to use
    endpoint = f'/api/v1/distros/{dist_name}/{dist_version}/projects'
    plan = await OracleClient().post(endpoint, source_rpms)
    repositories = jmespath.search('packages[].packages[].repositories[]', plan)
    repo_mapping = {f'{item["arch"]}-{item["name"]}': item
                    for item in repositories}
    return {
        'plan': plan,
        'repositories': list(repo_mapping.values())
    }


async def execute_release_plan(release_id: int, db: Session):
    repo_names = []
    repo_arches = []
    repositories_layout = dict()
    pulp_client = PulpClient(
        settings.pulp_host,
        settings.pulp_user,
        settings.pulp_password
    )
    packages_fields = ['name', 'epoch', 'version', 'release', 'arch']
    async with db.begin():
        release_result = await db.execute(
            select(models.Release).where(models.Release.id == release_id))
        release = release_result.scalars().first()
        if not release.plan.get('plan') or not release.plan.get('repositories'):
            raise EmptyReleasePlan('Cannot execute plan with empty body '
                                   'or repositories: {plan}, {repositories}'
                                   .format_map(release.plan))
        builds_q = select(models.Build).where(
            models.Build.id.in_(release.build_ids).options(
                selectinload(models.Build.source_rpms)
                    .selectinload(models.SourceRpm.artifact),
                selectinload(models.Build.binary_rpms)
                    .selectinload(models.BinaryRpm.artifact)
            )
        )
        build_result = await db.execute(builds_q)
        builds = build_result.scalars().all

        for repo in release.plan['repositories']:
            repo_names.append(repo['name'])
            repo_arches.append(repo['arch'])
        repo_q = select(models.Repository).where(
            models.Repository.name.in_(list(set(repo_names))),
            models.Repository.arch.in_(list(set(repo_arches))),
            models.Repository.production.is_(True))
        result = await db.execute(repo_q)
        repositories = result.scalars().all()

    for repo in repositories:
        if repo.name not in repositories_layout:
            repositories_layout[repo.name] = {'architectures': [],
                                              'packages_by_arch': {}}
            repositories_layout[repo.name]['architectures'].append(repo.arch)

    # First step: map packages from releases to actual packages we have in DB
    # Need to get information from Pulp
    db_binary_pkgs = {}
    db_source_packages = {}
    pulp_binary_pkgs = {}
    pulp_source_packages = {}
    for build in builds:
        for pkg in build.binary_rpms:
            pkg_info = await pulp_client.get_rpm_package(
                pkg.artifact.href, include_fields=packages_fields)
            db_binary_pkgs[pkg.artifact.name] = pkg
            pulp_binary_pkgs[pkg.artifact.name] = pkg_info

        for pkg in build.source_rpms:
            pkg_info = await pulp_client.get_rpm_package(
                pkg.artifact.href, include_fields=packages_fields)
            db_source_packages[pkg.artifact.name] = pkg
            pulp_source_packages[pkg.artifact.name] = pkg_info

    # Second step: make match between packages from plan and packages from Pulp
    for package_name, package_info in pulp_binary_pkgs.items():
        # The main difference is in release, so try to match package
        # by name-version-arch
        pkg_name = package_info['name']
        pkg_version = package_info['version']
        pkg_arch = package_info['arch']
        # FIXME: not working right now
        packages = jmespath.search(f'plan.packages[].packages[?name=="{pkg_name}"]', release.plan)
        