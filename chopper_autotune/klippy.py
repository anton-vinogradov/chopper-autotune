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
# the module that streams a section's samples, where it is not the section type:
# [lis3dh] is a chip variant served by lis2dw.py (Klipper and Kalico)
ACCEL_ENDPOINTS = {'lis3dh': 'lis2dw'}
OUTPUT_KEY = 'gcode_output'
STATUS_KEY = 'chopper_status'
OUTPUT_MAX = 256           # the console subscription is broadcast and permanent: keep a tail


class KlippyError(RuntimeError):
    pass


def fence_markers(token: str) -> 'tuple[str, str]':
    """The ECHO lines around a script whose console output is captured. ECHO is an
    extended command: Klipper parses its arguments as KEY=VALUE only, a bare word is a
    'Malformed command' that aborts the whole script. The token must not contain
    spaces, quotes or '#', ';', '*' (comment and checksum characters)."""
    return 'ECHO %s=BEGIN' % token, 'ECHO %s=END' % token


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

    def __init__(self, path: str, timeout: float = 600.0, buffer_sec: float = 15.0):
        self.path = path
        self.timeout = timeout
        self.buffer_sec = buffer_sec
        self.sock = None
        self.overflows = 0
        self._lock = threading.Lock()
        self._wakeup = threading.Condition(self._lock)
        self._responses = {}
        self._samples = deque()
        self._accel_chip = None
        self._accel_chips = set()
        self._output = deque(maxlen=OUTPUT_MAX)
        self._output_subscribed = False
        self._status = {}
        self._status_request = None
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
        """A dead reader must always flip _closed, or every request would hang a full timeout."""
        buffer = b''
        try:
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
                    try:
                        self._dispatch(json.loads(raw))
                    except Exception as e:
                        print('klippy: ignoring malformed message (%s)' % e)
        finally:
            with self._wakeup:
                self._closed = True
                self._wakeup.notify_all()

    def _dispatch(self, message: dict):
        if message.get('key') == ACCEL_KEY:
            data = message['params'].get('data')
            with self._wakeup:
                if message.get('chip') != self._accel_chip:
                    return              # a chip subscribed earlier: it streams until we close
                self.overflows += message['params'].get('overflows', 0)
                if data:
                    self._samples.extend(data)
                    horizon = self._samples[-1][0] - self.buffer_sec
                    while self._samples and self._samples[0][0] < horizon:
                        self._samples.popleft()
                self._wakeup.notify_all()
        elif message.get('key') == OUTPUT_KEY:
            with self._wakeup:
                self._output.append(str(message['params'].get('response', '')))
        elif message.get('key') == STATUS_KEY:
            with self._wakeup:                      # pushes carry only the changed fields
                for name, fields in message['params'].get('status', {}).items():
                    self._status.setdefault(name, {}).update(fields)
        elif 'id' in message:
            with self._wakeup:
                if message['id'] == self._status_request and 'result' in message:
                    # seeded here, in arrival order: a push right behind this response
                    # must land on top of it, not be overwritten by it
                    self._status = {name: dict(fields)
                                    for name, fields in message['result'].get('status', {}).items()}
                self._responses[message['id']] = message
                self._wakeup.notify_all()

    def request(self, method: str, params: 'dict | None' = None, status_base: bool = False) -> dict:
        """status_base: this response seeds the status copy (see subscribe_status)."""
        with self._lock:
            self._next_id += 1
            request_id = self._next_id
            if status_base:
                self._status_request = request_id
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

    def gcode_output(self, script: str) -> 'list[str]':
        """Run a script and return the console lines it printed (DUMP_TMC, QUERY_*...).
        The console subscription is shared by every client and console lines travel
        the same socket ahead of the script's own response, so the script is fenced
        with ECHO markers and only the lines between them are returned."""
        if not self._output_subscribed:
            self.request('gcode/subscribe_output', {'response_template': {'key': OUTPUT_KEY}})
            self._output_subscribed = True
        with self._lock:
            self._next_id += 1
            token = 'CHOPPER-%d-%d' % (os.getpid(), self._next_id)
        begin, end = fence_markers(token)
        with self._wakeup:
            self._output.clear()
        self.gcode('%s\n%s\n%s' % (begin, script, end))
        with self._wakeup:
            lines = list(self._output)
            self._output.clear()
        begun = ended = False
        fenced = []
        for line in lines:
            text = line.strip().lstrip('/').strip()      # console lines carry a '// ' prefix
            if text == begin:
                begun, fenced = True, []
            elif text == end:
                ended = begun
                break
            elif begun:
                fenced.append(line)
        if not ended:
            raise KlippyError('console fence %s not seen (%d console lines captured)'
                              % ('END' if begun else 'BEGIN', len(lines)))
        return fenced

    def subscribe_status(self, objects: 'dict[str, list[str]]'):
        """Keep a live copy of printer object fields: Klipper pushes their changes on this
        connection (every 0.25 s), so status() needs no round trip — an objects/query
        waits for Klipper's next 0.25 s tick. One subscription per connection."""
        self.request('objects/subscribe',
                     {'objects': objects, 'response_template': {'key': STATUS_KEY}}, status_base=True)

    def status(self) -> dict:
        with self._wakeup:
            return {name: dict(fields) for name, fields in self._status.items()}

    def settings(self) -> dict:
        result = self.request('objects/query', {'objects': {'configfile': ['settings']}})
        return result['status']['configfile']['settings']

    def object_list(self) -> 'list[str]':
        return self.request('objects/list')['objects']

    def info(self) -> dict:
        return self.request('info')

    def print_time(self) -> float:
        result = self.request('objects/query', {'objects': {'toolhead': ['print_time']}})
        return float(result['status']['toolhead']['print_time'])

    def stepper_states(self) -> 'dict[str, bool]':
        """Every stepper Klipper can enable, with its state (stepper_enable status)."""
        result = self.request('objects/query', {'objects': {'stepper_enable': ['steppers']}})
        return result['status']['stepper_enable']['steppers']

    def homed_axes(self) -> str:
        result = self.request('objects/query', {'objects': {'toolhead': ['homed_axes']}})
        return result['status']['toolhead']['homed_axes']

    def is_printing(self) -> bool:
        """True while a job is printing or paused (print_stats needs [virtual_sdcard];
        without it there is no job state to protect and False is returned)."""
        result = self.request('objects/query', {'objects': {'print_stats': ['state']}})
        state = (result['status'].get('print_stats') or {}).get('state')
        return state in ('printing', 'paused')

    def subscribe_accel(self, accel_chip: str):
        """Chip section like 'adxl345', 'adxl345 head' or 'lis3dh' -> '<module>/dump_<module>'
        endpoint, with the section's last word as the sensor name. From here on the
        sample buffer holds this chip only.

        Once per chip and connection: Klipper streams until the connection closes, and
        since v0.12.0-53 (bulk_sensor.py, Kalico too) it sends each batch once per
        subscribe request — tune subscribed in the scan and again in the descent, and
        every sample arrived twice (four times for motor B of AXIS=xy). A chip subscribed
        earlier keeps streaming; the chip name in the response template tells its
        batches apart."""
        with self._wakeup:
            if accel_chip != self._accel_chip:
                self._accel_chip = accel_chip
                self._samples.clear()
        if accel_chip in self._accel_chips:
            return
        parts = accel_chip.split()
        chip, sensor = ACCEL_ENDPOINTS.get(parts[0], parts[0]), parts[-1]
        self.request('%s/dump_%s' % (chip, chip),
                     {'sensor': sensor, 'response_template': {'key': ACCEL_KEY, 'chip': accel_chip}})
        self._accel_chips.add(accel_chip)

    def samples_between(self, start: float, end: float) -> 'list[list[float]]':
        """Walk from the tail: the window of interest is always recent, the buffer is sorted."""
        out = []
        with self._lock:
            for sample in reversed(self._samples):
                if sample[0] > end:
                    continue
                if sample[0] < start:
                    break
                out.append(sample)
        out.reverse()
        return out

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
