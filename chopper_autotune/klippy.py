"""Direct client for the Klipper API server: unix socket, JSON messages framed by 0x03."""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from collections import deque

SOCKET_CANDIDATES = ('~/printer_data/comms/klippy.sock', '/tmp/klippy_uds')
SEPARATOR = b'\x03'
ACCEL_KEY = 'accel'


class KlippyError(RuntimeError):
    pass


def find_socket(explicit: 'str | None' = None) -> str:
    candidates = (explicit,) if explicit else SOCKET_CANDIDATES
    for candidate in candidates:
        path = os.path.expanduser(candidate)
        if os.path.exists(path):
            return path
    raise KlippyError('klippy socket not found (tried %s), pass --socket' % ', '.join(candidates))


class Klippy:
    """Request/response plus a rolling buffer of streamed accelerometer samples.

    A reader thread demultiplexes socket traffic: responses are matched to requests
    by id, subscription batches (marked by the response_template key) go into the
    sample buffer.
    """

    def __init__(self, path: str, timeout: float = 600.0, buffer_sec: float = 120.0):
        self.path = path
        self.timeout = timeout
        self.buffer_sec = buffer_sec
        self.sock = None
        self.overflows = 0
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._responses = {}
        self._samples = deque()
        self._next_id = 0
        self._closed = False

    def connect(self, sock: 'socket.socket | None' = None) -> 'Klippy':
        if sock is None:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self.path)
        self.sock = sock
        threading.Thread(target=self._read_loop, daemon=True).start()
        return self

    def close(self):
        with self._wakeup:
            self._closed = True
            self._wakeup.notify_all()
        if self.sock is not None:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.sock.close()

    def _read_loop(self):
        buffer = b''
        while True:
            try:
                chunk = self.sock.recv(65536)
            except OSError:
                break
            if not chunk:
                break
            buffer += chunk
            *messages, buffer = buffer.split(SEPARATOR)
            for raw in messages:
                self._dispatch(json.loads(raw))
        with self._wakeup:
            self._closed = True
            self._wakeup.notify_all()

    def _dispatch(self, message: dict):
        if message.get('key') == ACCEL_KEY:
            params = message['params']
            with self._wakeup:
                self.overflows += params.get('overflows', 0)
                self._samples.extend(params['data'])
                horizon = self._samples[-1][0] - self.buffer_sec
                while self._samples and self._samples[0][0] < horizon:
                    self._samples.popleft()
                self._wakeup.notify_all()
        elif 'id' in message:
            with self._wakeup:
                self._responses[message['id']] = message
                self._wakeup.notify_all()

    def request(self, method: str, params: 'dict | None' = None) -> dict:
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
        payload = json.dumps({'id': request_id, 'method': method, 'params': params or {}})
        self.sock.sendall(payload.encode() + SEPARATOR)
        deadline = time.monotonic() + self.timeout
        with self._wakeup:
            while request_id not in self._responses:
                if self._closed:
                    raise KlippyError('klippy connection closed')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise KlippyError('%s timed out after %.0fs' % (method, self.timeout))
                self._wakeup.wait(remaining)
            message = self._responses.pop(request_id)
        if 'error' in message:
            error = message['error']
            raise KlippyError('%s failed: %s' % (method, error.get('message', error)))
        return message['result']

    def gcode(self, script: str):
        return self.request('gcode/script', {'script': script})

    def settings(self) -> dict:
        result = self.request('objects/query', {'objects': {'configfile': ['settings']}})
        return result['status']['configfile']['settings']

    def info(self) -> dict:
        return self.request('info')

    def print_time(self) -> float:
        result = self.request('objects/query', {'objects': {'toolhead': ['print_time']}})
        return float(result['status']['toolhead']['print_time'])

    def subscribe_accel(self, accel_chip: str):
        """Chip section like 'adxl345', 'adxl345 head' or 'lis2dw' -> '<type>/dump_<type>' endpoint."""
        parts = accel_chip.split()
        chip, sensor = parts[0], parts[-1]
        self.request('%s/dump_%s' % (chip, chip),
                     {'sensor': sensor, 'response_template': {'key': ACCEL_KEY}})

    def samples_between(self, start: float, end: float) -> 'list[list[float]]':
        with self._lock:
            return [s for s in self._samples if start <= s[0] <= end]

    def wait_for_sample(self, t: float, timeout: float = 5.0):
        """Batches are flushed with a delay; wait until the stream catches up with print time t."""
        deadline = time.monotonic() + timeout
        with self._wakeup:
            while not (self._samples and self._samples[-1][0] >= t):
                if self._closed:
                    raise KlippyError('klippy connection closed')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise KlippyError('accelerometer stream stalled, no samples past %.3f' % t)
                self._wakeup.wait(remaining)
