import typing
import urllib.parse

import aiohttp


__all__ = ['AltsClient']


class AltsClient:
    def __init__(self, base_url: str, token: str):
        self._base_url = base_url
        self._headers = {'authorization': f'Bearer {token}'}

    async def schedule_task(self, dist_name: str,
                            dist_version: typing.Union[int, str],
                            dist_arch: str, package_name: str,
                            package_version: str, callback_href: str,
                            package_release: str = None,
                            repositories: typing.List[dict] = None,
                            module_name: str = None,
                            module_stream: str = None,
                            module_version: str = None):
        if package_release:
            full_version = f'{package_version}-{package_release}'
        else:
            full_version = package_version
        payload = {
            'runner_type': 'docker',
            'dist_name': dist_name,
            'dist_version': dist_version,
            'dist_arch': dist_arch,
            'package_name': package_name,
            'package_version': full_version,
            'callback_href': callback_href,
        }
        if module_name and module_stream and module_version:
            payload['module_name'] = module_name
            payload['module_stream'] = module_stream
            payload['module_version'] = module_version
        if repositories:
            payload['repositories'] = repositories

        print(f'TS payload: {payload}')
        full_url = urllib.parse.urljoin(self._base_url, '/tasks/schedule')
        async with aiohttp.ClientSession(headers=self._headers) as session:
            async with session.post(full_url, json=payload) as response:
                resp_json = await response.json()
                response.raise_for_status()
                return resp_json
