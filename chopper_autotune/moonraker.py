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

    def settings(self) -> dict:
        result = self._request('GET', '/printer/objects/query', {'configfile': 'settings'})
        return result['status']['configfile']['settings']

    def info(self) -> dict:
        return self._request('GET', '/printer/info')

    def set_tmc_fields(self, stepper: str, fields: dict):
        self.gcode(tmc.set_fields_script(stepper, fields))
