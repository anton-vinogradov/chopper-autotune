import json
import socket
import threading

import pytest

from chopper_autotune.klippy import Klippy, KlippyError, find_socket


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
        for batch in self.batches:
            send(self.server_sock, {'key': 'accel', 'params': batch})


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


def test_gcode_output_returns_only_the_fenced_lines():
    kl, server = make_pair()

    def serve():
        buffer = b''
        seen = 0
        while seen < 2:
            buffer += server.recv(4096)
            while b'\x03' in buffer:
                raw, buffer = buffer.split(b'\x03', 1)
                request = json.loads(raw)
                seen += 1
                if request['method'] == 'gcode/script':
                    # a foreign console line first, then the script's own output: Klipper
                    # prefixes every line with '// ' and the ECHO markers come back as such
                    send(server, {'key': 'gcode_output', 'params': {'response': '// someone else'}})
                    for line in request['params']['script'].split('\n'):
                        if line.startswith('ECHO '):
                            send(server, {'key': 'gcode_output', 'params': {'response': '// ' + line}})
                        else:
                            send(server, {'key': 'gcode_output',
                                          'params': {'response': '// GCONF:      0000000e en_pwm_mode=1'}})
                send(server, {'id': request['id'], 'result': {}})

    threading.Thread(target=serve, daemon=True).start()
    lines = kl.gcode_output('DUMP_TMC STEPPER=stepper_x REGISTER=GCONF')
    assert lines == ['// GCONF:      0000000e en_pwm_mode=1']
    kl.close()
