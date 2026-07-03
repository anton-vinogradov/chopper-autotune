"""Minimal Moonraker HTTP client, stdlib only."""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

from . import tmc


class MoonrakerError(RuntimeError):
    pass


class Moonraker:
    def __init__(self, url: str = 'http://127.0.0.1:7125', timeout: float = 600.0):
        self.url = url.rstrip('/')
        self.timeout = timeout

    def _request(self, method: str, path: str, params: dict = None):
        url = self.url + path
        data = None
        if params is not None:
            if method == 'GET':
                url += '?' + urllib.parse.urlencode(params)
            else:
                data = urllib.parse.urlencode(params).encode()
        req = urllib.request.Request(url, data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.load(resp)
        except urllib.error.HTTPError as e:
            try:
                detail = json.load(e)['error']['message']
            except Exception:
                detail = str(e)
            raise MoonrakerError('%s %s failed: %s' % (method, path, detail)) from e
        except OSError as e:
            raise MoonrakerError('cannot reach Moonraker at %s: %s' % (self.url, e)) from e
        return payload['result']

    def gcode(self, script: str):
        return self._request('POST', '/printer/gcode/script', {'script': script})

    def set_tmc_fields(self, stepper: str, fields: dict):
        self.gcode(tmc.set_fields_script(stepper, fields))

    def is_printing(self) -> bool:
        result = self._request('GET', '/printer/objects/query', {'print_stats': 'state'})
        return result['status'].get('print_stats', {}).get('state') == 'printing'

    def list_config_files(self) -> 'list[str]':
        result = self._request('GET', '/server/files/list', {'root': 'config'})
        return [item['path'] for item in result if item['path'].endswith('.cfg')]

    def download_config(self, name: str) -> str:
        req = urllib.request.Request('%s/server/files/config/%s' % (self.url, urllib.parse.quote(name)))
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.read().decode()
        except (urllib.error.HTTPError, OSError) as e:
            raise MoonrakerError('cannot download %s: %s' % (name, e)) from e

    def upload_config(self, name: str, content: str):
        boundary = '----chopper-autotune-boundary'
        body = ''.join([
            '--%s\r\nContent-Disposition: form-data; name="root"\r\n\r\nconfig\r\n' % boundary,
            '--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
            'Content-Type: text/plain\r\n\r\n%s\r\n' % (boundary, name, content),
            '--%s--\r\n' % boundary,
        ]).encode()
        req = urllib.request.Request(
            self.url + '/server/files/upload', data=body, method='POST',
            headers={'Content-Type': 'multipart/form-data; boundary=%s' % boundary})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                resp.read()
        except (urllib.error.HTTPError, OSError) as e:
            raise MoonrakerError('cannot upload %s: %s' % (name, e)) from e
