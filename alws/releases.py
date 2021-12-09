import re
import typing
from collections import namedtuple

import jmespath
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from alws import models
from alws.config import settings
from alws.utils.beholder_client import BeholderClient
from alws.utils.pulp_client import PulpClient


__all__ = [
    'execute_release_plan',
    'get_release_plan',
    'EmptyReleasePlan',
    'MissingRepository',
]


class EmptyReleasePlan(ValueError):
    pass


class MissingRepository(ValueError):
    pass


RepoType = namedtuple('RepoType', ('name', 'arch', 'debug'))


async def __get_pulp_packages(db: Session, build_ids: typing.List[int]) \
        -> typing.Tuple[typing.List[dict], typing.List[str]]:
    src_rpm_names = []
    packages_fields = ['name', 'epoch', 'version', 'release', 'arch']
    pulp_packages = []
    pulp_client = PulpClient(
        settings.pulp_host,
        settings.pulp_user,
        settings.pulp_password
    )

    builds_q = select(models.Build).where(
        models.Build.id.in_(build_ids)).options(
        selectinload(
            models.Build.source_rpms).selectinload(
            models.SourceRpm.artifact),
        selectinload(
            models.Build.binary_rpms).selectinload(
            models.BinaryRpm.artifact)
    )
    build_result = await db.execute(builds_q)
    for build in build_result.scalars().all():
        for src_rpm in build.source_rpms:
            # Failsafe to not process logs
            if src_rpm.artifact.type != 'rpm':
                continue
            src_rpm_names.append(src_rpm.artifact.name)
            pkg_info = await pulp_client.get_rpm_package(
                src_rpm.artifact.href, include_fields=packages_fields)
            pkg_info['artifact_href'] = src_rpm.artifact.href
            pkg_info['full_name'] = src_rpm.artifact.name
            pulp_packages.append(pkg_info)
        for binary_rpm in build.binary_rpms:
            # Failsafe to not process logs
            if binary_rpm.artifact.type != 'rpm':
                continue
            pkg_info = await pulp_client.get_rpm_package(
                binary_rpm.artifact.href, include_fields=packages_fields)
            pkg_info['artifact_href'] = binary_rpm.artifact.href
            pkg_info['full_name'] = binary_rpm.artifact.name
            pulp_packages.append(pkg_info)
    return pulp_packages, src_rpm_names


async def get_release_plan(db: Session, build_ids: typing.List[int],
                           base_dist_name: str, base_dist_version: str,
                           reference_dist_name: str,
                           reference_dist_version: str) -> dict:
    clean_ref_dist_name = re.search(
        r'(?P<dist_name>[a-z]+)', reference_dist_name,
        re.IGNORECASE).groupdict().get('dist_name')
    clean_ref_dist_name_lower = clean_ref_dist_name.lower()
    endpoint = f'/api/v1/distros/{clean_ref_dist_name}/' \
               f'{reference_dist_version}/projects/'
    packages = []
    repo_name_regex = re.compile(r'\w+-\d-(?P<name>\w+(-\w+)?)')
    pulp_packages, src_rpm_names = await __get_pulp_packages(db, build_ids)
    repo_q = select(models.Repository).where(
        models.Repository.production.is_(True))
    result = await db.execute(repo_q)
    prod_repos = [
        {
            'id': repo.id,
            'name': repo.name,
            'arch': repo.arch,
            'debug': repo.debug
        }
        for repo in result.scalars().all()
    ]

    repos_mapping = {RepoType(repo['name'], repo['arch'], repo['debug']): repo
                     for repo in prod_repos}

    if not settings.package_oracle_enabled:
        return {
            'packages': [{'package': pkg, 'repositories': []}
                         for pkg in pulp_packages],
            'repositories': prod_repos
        }

    oracle_response = await BeholderClient(settings.packages_oracle_host).post(
        endpoint, src_rpm_names)
    if oracle_response.get('packages', []):
        for package in pulp_packages:
            pkg_name = package['name']
            pkg_version = package['version']
            pkg_arch = package['arch']
            query = f'packages[].packages[?name==\'{pkg_name}\' ' \
                    f'&& version==\'{pkg_version}\' ' \
                    f'&& arch==\'{pkg_arch}\'][]'
            predicted_package = jmespath.search(query, oracle_response)
            pkg_info = {'package': package, 'repositories': []}
            if predicted_package:
                # JMESPath will find a list with 1 element inside
                predicted_package = predicted_package[0]
                repositories = predicted_package['repositories']
                release_repositories = set()
                for repo in repositories:
                    ref_repo_name = repo['name']
                    repo_name = (repo_name_regex.search(ref_repo_name)
                                 .groupdict()['name'])
                    release_repo_name = (f'{clean_ref_dist_name_lower}'
                                         f'-{base_dist_version}-{repo_name}')
                    debug = ref_repo_name.endswith('debuginfo')
                    if repo['arch'] == 'src':
                        debug = False
                    release_repo = RepoType(
                        release_repo_name, repo['arch'], debug)
                    release_repositories.add(release_repo)
                pkg_info['repositories'] = [
                    repos_mapping.get(item) for item in release_repositories]
                with open('test_repo.log', 'a') as logs:
                    logs.write(f"pkg_info[repositories]: {pkg_info['repositories']}\n")
            packages.append(pkg_info)
    return {
        'packages': packages,
        'repositories': prod_repos
    }


async def execute_release_plan(release_id: int, db: Session):
    packages_to_repo_layout = {}

    async with db.begin():
        release_result = await db.execute(
            select(models.Release).where(models.Release.id == release_id))
        release = release_result.scalars().first()
        if not release.plan.get('packages') or \
                not release.plan.get('repositories'):
            raise EmptyReleasePlan('Cannot execute plan with empty packages '
                                   'or repositories: {packages}, {repositories}'
                                   .format_map(release.plan))

    for package in release.plan['packages']:
        for repository in package['repositories']:
            repo_name = repository['name']
            repo_arch = repository['arch']
            if repo_name not in packages_to_repo_layout:
                packages_to_repo_layout[repo_name] = {}
            if repo_arch not in packages_to_repo_layout[repo_name]:
                packages_to_repo_layout[repo_name][repo_arch] = []
            packages_to_repo_layout[repo_name][repo_arch].append(
                package['package']['artifact_href'])

    pulp_client = PulpClient(
        settings.pulp_host,
        settings.pulp_user,
        settings.pulp_password
    )
    repo_status = {}

    for repository_name, arches in packages_to_repo_layout.items():
        repo_status[repository_name] = {}
        for arch, packages in arches.items():
            repo_q = select(models.Repository).where(
                models.Repository.name == repository_name,
                models.Repository.arch == arch)
            repo_result = await db.execute(repo_q)
            repo = repo_result.scalars().first()
            if not repo:
                raise MissingRepository(
                    f'Repository with name {repository_name} is missing '
                    f'or doesn\'t have pulp_href field')
            result = await pulp_client.modify_repository(
                repo.pulp_href, add=packages)
            repo_status[repository_name][arch] = result

    return repo_status
