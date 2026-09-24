import json
import socket
import threading
import time

import pytest

import fake_klipper
from chopper_autotune.klippy import Klippy, KlippyError, accel_batch, fence_markers, find_socket


def make_pair():
    client_sock, server_sock = socket.socketpair()
    kl = Klippy(path='<test>', timeout=5.0)
    kl.connect(sock=client_sock)
    return kl, server_sock


def send(server_sock, message: dict):
    server_sock.sendall(json.dumps(message).encode() + b'\x03')


class Responder(threading.Thread):
    """Replies to each request with a canned result, then streams canned batches."""

    def __init__(self, server_sock, result, batches=()):
        super().__init__(daemon=True)
        self.server_sock = server_sock
        self.result = result
        self.batches = batches

    def run(self):
        buffer = b''
        while b'\x03' not in buffer:
            buffer += self.server_sock.recv(4096)
        request = json.loads(buffer.split(b'\x03')[0])
        send(self.server_sock, {'id': request['id'], 'result': self.result})
        template = request['params'].get('response_template', {})
        for batch in self.batches:
            send(self.server_sock, dict(template, params=batch))


class BulkSensor(threading.Thread):
    """Klipper's bulk_sensor.py behind the API server: every dump request adds one more
    client, and a batch goes to every client of its sensor."""

    def __init__(self, server_sock):
        super().__init__(daemon=True)
        self.server_sock = server_sock
        self.requests = []
        self.clients = {}

    def run(self):
        buffer = b''
        while True:
            chunk = self.server_sock.recv(4096)
            if not chunk:
                return
            buffer += chunk
            while b'\x03' in buffer:
                raw, buffer = buffer.split(b'\x03', 1)
                request = json.loads(raw)
                sensor = request['params']['sensor']
                self.requests.append((request['method'], sensor))
                self.clients.setdefault(sensor, []).append(request['params']['response_template'])
                send(self.server_sock, {'id': request['id'], 'result': {}})

    def push(self, sensor, data):
        for template in self.clients.get(sensor, []):
            send(self.server_sock, dict(template, params={'data': data, 'overflows': 0}))


def test_request_response_roundtrip():
    kl, server_sock = make_pair()
    Responder(server_sock, {'state': 'ready'}).start()
    assert kl.request('info') == {'state': 'ready'}
    kl.close()


def test_error_response_raises():
    kl, server_sock = make_pair()
    threading.Thread(target=lambda: (server_sock.recv(4096), send(
        server_sock, {'id': 1, 'error': {'message': 'Must home axis first'}})), daemon=True).start()
    with pytest.raises(KlippyError, match='Must home axis first'):
        kl.request('gcode/script', {'script': 'G1 X200'})
    kl.close()


def test_batches_and_sample_window():
    kl, server_sock = make_pair()
    batches = [
        {'overflows': 0, 'data': [[1.0, 1, 2, 3], [1.5, 4, 5, 6]]},
        {'overflows': 2, 'data': [[2.0, 7, 8, 9], [2.5, 1, 1, 1]]},
    ]
    Responder(server_sock, {'header': ['time', 'x', 'y', 'z']}, batches).start()
    kl.subscribe_accel('adxl345 head')
    kl.wait_for_sample(2.5, timeout=2.0)

    assert kl.overflows == 2
    assert [s[0] for s in kl.samples_between(1.2, 2.2)] == [1.5, 2.0]
    kl.close()


def test_a_chip_is_subscribed_once_per_connection():
    # tune scans and then descends on one connection, both subscribing: Klipper sent
    # every batch once per request, so each sample reached the buffer twice
    kl, server = make_pair()
    klipper = BulkSensor(server)
    klipper.start()
    kl.subscribe_accel('adxl345')
    kl.subscribe_accel('adxl345')
    batch = [[1.0, 1, 2, 3], [1.5, 4, 5, 6]]
    klipper.push('adxl345', batch)
    klipper.push('adxl345', [[2.0, 7, 8, 9]])       # all copies of the first batch are in
    kl.wait_for_sample(2.0, timeout=2.0)

    assert klipper.requests == [('adxl345/dump_adxl345', 'adxl345')]
    assert kl.samples_between(0.0, 1.9) == batch
    kl.close()


def test_the_buffer_holds_the_chip_subscribed_last():
    # tune AXIS=xy with accel_chip_x and accel_chip_y: the first motor's chip streams on
    # after the second one is subscribed, and both landed in one buffer
    kl, server = make_pair()
    klipper = BulkSensor(server)
    klipper.start()
    kl.subscribe_accel('adxl345 hotend')
    kl.subscribe_accel('adxl345 bed')
    klipper.push('hotend', [[1.0, 9, 9, 9]])
    klipper.push('bed', [[1.0, 1, 1, 1], [1.5, 2, 2, 2]])
    kl.wait_for_sample(1.5, timeout=2.0)
    assert kl.samples_between(0.0, 2.0) == [[1.0, 1, 1, 1], [1.5, 2, 2, 2]]

    kl.subscribe_accel('adxl345 hotend')            # back to the first chip: no new request
    klipper.push('bed', [[3.0, 2, 2, 2]])
    klipper.push('hotend', [[3.0, 9, 9, 9]])
    kl.wait_for_sample(3.0, timeout=2.0)
    assert kl.samples_between(0.0, 4.0) == [[3.0, 9, 9, 9]]
    assert klipper.requests == [('adxl345/dump_adxl345', 'hotend'), ('adxl345/dump_adxl345', 'bed')]
    kl.close()


def test_beacon_batches_are_bare_sample_lists():
    # Beacon sends a header line ahead of the response, then no 'data'/'overflows' dict
    kl, server_sock = make_pair()
    requests = []

    def serve():
        request = json.loads(server_sock.recv(4096).split(b'\x03')[0])
        requests.append(request)
        template = request['params']['response_template']
        send(server_sock, {'header': ['time', 'x', 'y', 'z']})
        send(server_sock, {'id': request['id'], 'result': {}})
        send(server_sock, dict(template, params=[[1.0, 1, 2, 3], [1.5, 4, 5, 6]]))
        send(server_sock, dict(template, params=[[2.0, 7, 8, 9], None, [2.5, 1, 1, 1]]))
    threading.Thread(target=serve, daemon=True).start()
    kl.subscribe_accel('beacon')
    kl.wait_for_sample(2.5, timeout=2.0)

    assert 'sensor' not in requests[0]['params']        # Beacon refuses 'beacon'
    assert [s[0] for s in kl.samples_between(1.2, 2.2)] == [1.5, 2.0]
    assert kl.overflows == 1                 # the sample Beacon could not decode
    kl.close()


@pytest.mark.parametrize('params, batch', [
    ({'data': [[1.0, 1, 2, 3]], 'overflows': 2}, ([[1.0, 1, 2, 3]], 2)),
    ({'data': []}, ([], 0)),
    ([[1.0, 1, 2, 3], None], ([[1.0, 1, 2, 3]], 1)),
    ([], ([], 0)),
])
def test_accel_batch_reads_both_shapes(params, batch):
    assert accel_batch(params) == batch


@pytest.mark.parametrize('section, endpoint, sensor', [
    ('adxl345', 'adxl345/dump_adxl345', 'adxl345'),
    ('adxl345 head', 'adxl345/dump_adxl345', 'head'),
    ('lis3dh', 'lis2dw/dump_lis2dw', 'lis3dh'),              # lis2dw.py serves [lis3dh]
    ('bmi160 toolhead', 'bmi160/dump_bmi160', 'toolhead'),
    ('beacon', 'beacon/dump_accel', None),                   # Beacon refuses 'beacon'
    ('beacon sensor tool', 'beacon/dump_accel', 'tool'),
])
def test_the_accelerometer_stream_endpoint(section, endpoint, sensor):
    kl = Klippy('<test>')
    calls = []
    kl.request = lambda method, params: calls.append((method, params.get('sensor')))
    kl.subscribe_accel(section)
    assert calls == [(endpoint, sensor)]


def test_malformed_frame_does_not_kill_reader():
    kl, server_sock = make_pair()

    def reply():
        server_sock.recv(4096)
        server_sock.sendall(b'not json\x03')
        send(server_sock, {'key': 'accel', 'params': {'data': []}})
        send(server_sock, {'id': 1, 'result': {'ok': True}})

    threading.Thread(target=reply, daemon=True).start()
    assert kl.request('info') == {'ok': True}
    kl.close()


def test_framing_split_across_chunks():
    kl, server_sock = make_pair()
    payload = json.dumps({'id': 1, 'result': {'ok': True}}).encode() + b'\x03'

    def reply():
        server_sock.recv(4096)
        server_sock.sendall(payload[:7])
        server_sock.sendall(payload[7:])

    threading.Thread(target=reply, daemon=True).start()
    assert kl.request('info') == {'ok': True}
    kl.close()


def test_closed_connection_raises():
    kl, server_sock = make_pair()
    threading.Thread(target=lambda: (server_sock.recv(4096), server_sock.close()), daemon=True).start()
    with pytest.raises(KlippyError, match='closed'):
        kl.request('info')


def test_find_socket(tmp_path):
    path = tmp_path / 'klippy.sock'
    path.touch()
    assert find_socket(str(path)) == str(path)
    with pytest.raises(KlippyError):
        find_socket(str(tmp_path / 'missing.sock'))


def serve_scripts(server, respond, foreign=()):
    """Answer requests like Klipper (tests/fake_klipper.py); `foreign` console lines
    from other clients land ahead of each script's own output."""
    def serve():
        buffer = b''
        while True:
            chunk = server.recv(4096)
            if not chunk:
                return
            buffer += chunk
            while b'\x03' in buffer:
                raw, buffer = buffer.split(b'\x03', 1)
                request = json.loads(raw)
                if request['method'] == 'gcode/script':
                    for text in foreign:
                        send(server, {'key': 'gcode_output', 'params': {'response': text}})
                    fake_klipper.run_script(server, request, respond)
                else:
                    send(server, {'id': request['id'], 'result': {}})
    threading.Thread(target=serve, daemon=True).start()


def test_gcode_output_returns_only_the_fenced_lines():
    kl, server = make_pair()
    serve_scripts(server, lambda line: ['// GCONF:      0000000e en_pwm_mode=1'],
                  foreign=['// someone else'])
    lines = kl.gcode_output('DUMP_TMC STEPPER=stepper_x REGISTER=GCONF')
    assert lines == ['// GCONF:      0000000e en_pwm_mode=1']
    kl.close()


def test_fence_markers_are_well_formed_extended_commands():
    # Klipper parses ECHO arguments as KEY=VALUE: the pre-fix bare-word marker aborted
    # every fenced script with "Malformed command" on real printers
    begin, end = fence_markers('CHOPPER-4242-7')
    assert not fake_klipper.malformed(begin) and not fake_klipper.malformed(end)
    assert fake_klipper.malformed('ECHO CHOPPER-7-BEGIN')


def test_gcode_output_raises_when_the_fence_is_lost():
    # a server that swallows the markers (an ECHO-less fork, a buffer overflow):
    # an explicit error, not an empty capture that reads as "no GCONF line"
    kl, server = make_pair()

    def serve():
        buffer = b''
        while True:
            chunk = server.recv(4096)
            if not chunk:
                return
            buffer += chunk
            while b'\x03' in buffer:
                raw, buffer = buffer.split(b'\x03', 1)
                request = json.loads(raw)
                if request['method'] == 'gcode/script':
                    send(server, {'key': 'gcode_output',
                                  'params': {'response': '// GCONF:      00000000'}})
                send(server, {'id': request['id'], 'result': {}})
    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(KlippyError, match='BEGIN not seen'):
        kl.gcode_output('DUMP_TMC STEPPER=stepper_x REGISTER=GCONF')
    kl.close()


def test_gcode_output_surfaces_klipper_errors():
    kl, server = make_pair()
    serve_scripts(server, lambda line: [])
    with pytest.raises(KlippyError, match='Malformed command'):
        kl.gcode_output('DUMP_TMC stepper_x')
    kl.close()


def test_status_subscription_keeps_a_live_copy_without_round_trips():
    # the thermal guard reads this before every move: Klipper pushes changed fields only
    kl, server = make_pair()

    def serve():
        buffer = b''
        while b'\x03' not in buffer:
            buffer += server.recv(4096)
        request = json.loads(buffer.split(b'\x03')[0])
        assert request['method'] == 'objects/subscribe'
        key = request['params']['response_template']['key']
        send(server, {'id': request['id'], 'result': {'eventtime': 1.0, 'status': {
            'tmc2240 stepper_x': {'drv_status': None, 'temperature': None}}}})
        send(server, {'key': key, 'params': {'eventtime': 1.25, 'status': {
            'tmc2240 stepper_x': {'drv_status': {'otpw': 1}}}}})
    threading.Thread(target=serve, daemon=True).start()
    kl.subscribe_status({'tmc2240 stepper_x': ['drv_status', 'temperature']})
    deadline = time.monotonic() + 2
    while kl.status()['tmc2240 stepper_x']['drv_status'] is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert kl.status() == {'tmc2240 stepper_x': {'drv_status': {'otpw': 1}, 'temperature': None}}
    kl.close()
