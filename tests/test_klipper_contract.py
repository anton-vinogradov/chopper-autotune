"""Contract with the REAL Klipper/Kalico G-code parser and API server. The fakes in the
other tests only repeat our own assumptions: a bare-word ECHO fence passed them and
was a 'Malformed command' on every printer. The GPL sources are fetched, never
committed (tests/fetch_klipper_sources.sh); CI sets CHOPPER_CONTRACT=1 so a missing
download fails instead of skipping."""
import importlib.util
import json
import math
import os
import select
import socket
import sys
import threading
import time
import types

import pytest

import fake_klipper
from chopper_autotune import tmc
from chopper_autotune.collect import live_stealth
from chopper_autotune.klippy import Klippy, KlippyError, fence_markers

# old Klipper releases carry regex strings Python 3.12+ warns about while compiling
pytestmark = pytest.mark.filterwarnings('ignore::SyntaxWarning')

SRC = os.environ.get('KLIPPER_SRC_DIR') or os.path.join(os.path.dirname(__file__), '.klipper-src')
# GCONF as a TMC2209 prints it after Klipper's own init (spreadCycle bit clear = stealthChop)
GCONF_STEALTH = 'GCONF:      000001c0 pdn_disable=1 mstep_reg_select=1 multistep_filt=1'
GCONF_SPREAD = 'GCONF:      000001c4 en_spreadcycle=1 pdn_disable=1 mstep_reg_select=1 multistep_filt=1'


def fetched(filename: str) -> 'list[str | None]':
    names = sorted(d for d in os.listdir(SRC)
                   if os.path.isfile(os.path.join(SRC, d, filename))) if os.path.isdir(SRC) else []
    return names or [None]


def require(source):
    if source is None:
        message = 'no Klipper sources in %s: run tests/fetch_klipper_sources.sh' % SRC
        if os.environ.get('CHOPPER_CONTRACT'):
            pytest.fail(message)
        pytest.skip(message)


def safe_float(value):
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError('%s is not a valid float' % value)
    return number


def load_gcode(source: str):
    path = os.path.join(SRC, source, 'gcode.py')
    tag = source.replace('.', '_').replace('-', '_')
    name = 'contract_%s_gcode' % tag
    if source.startswith('kalico'):
        # Kalico's gcode.py is a package module ('from . import mathutil'); the real
        # mathutil pulls in its logging machinery, the parser only needs safe_float
        package = types.ModuleType('contract_%s' % tag)
        package.__path__ = [os.path.dirname(path)]
        mathutil = types.ModuleType(package.__name__ + '.mathutil')
        mathutil.safe_float = safe_float
        package.mathutil = mathutil
        sys.modules[package.__name__] = package
        sys.modules[mathutil.__name__] = mathutil
        name = package.__name__ + '.gcode'
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_webhooks(source: str, gcode_module):
    saved = sys.modules.get('gcode')
    sys.modules['gcode'] = gcode_module             # webhooks.py does 'import gcode'
    try:
        name = 'contract_%s_webhooks' % source.replace('.', '_').replace('-', '_')
        spec = importlib.util.spec_from_file_location(name, os.path.join(SRC, source, 'webhooks.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if saved is None:
            sys.modules.pop('gcode', None)
        else:
            sys.modules['gcode'] = saved


class Reactor:
    def mutex(self):
        return threading.Lock()

    def monotonic(self):
        return time.monotonic()

    def register_fd(self, fd, read_cb, write_cb=None):
        return object()

    def unregister_fd(self, handle):
        pass

    def set_fd_wake(self, handle, is_readable=True, is_writeable=False):
        pass

    def register_callback(self, callback, waketime=None):
        callback(time.monotonic())


class Printer:
    config_error = RuntimeError

    def __init__(self):
        self.reactor = Reactor()
        self.objects = {}
        self.handlers = {}
        self.shutdowns = []

    def get_start_args(self):
        return {}

    def get_reactor(self):
        return self.reactor

    def register_event_handler(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def send_event(self, event, *args):
        return [callback(*args) for callback in self.handlers.get(event, [])]

    def invoke_shutdown(self, message):
        self.shutdowns.append(message)

    def get_state_message(self):
        return 'Printer is ready', 'ready'

    def set_rollover_info(self, *args, **kwargs):
        pass

    def lookup_object(self, name, default=None):
        return self.objects.get(name, default)


def ready_dispatch(gcode_module, gconf: str):
    """A ready GCodeDispatch with DUMP_TMC and SET_STEPPER_ENABLE registered the way
    klippy/extras/tmc.py and stepper_enable.py do (mux on STEPPER)."""
    printer = Printer()
    printer.command_error = gcode_module.CommandError     # as klippy.Printer has it
    dispatch = gcode_module.GCodeDispatch(printer)
    printer.objects['gcode'] = dispatch
    printer.send_event('klippy:ready')
    ran = []

    def dump_tmc(gcmd):
        ran.append(gcmd.get_commandline())
        gcmd.get('REGISTER')
        gcmd.respond_info(gconf)
    dispatch.register_mux_command('DUMP_TMC', 'STEPPER', 'stepper_x', dump_tmc)
    dispatch.register_mux_command('SET_STEPPER_ENABLE', 'STEPPER', 'stepper_x',
                                  lambda gcmd: ran.append(gcmd.get_commandline()))
    return printer, dispatch, ran


def bridge(dispatch, error_class):
    """The part of webhooks.py our client talks to, over a socketpair: output lines go
    out while the script runs, the response after it (the older releases have no
    webhooks.py fetched; the master one is exercised for real below)."""
    client, server = socket.socketpair()
    lock = threading.Lock()

    def send(message):
        with lock:
            server.sendall(json.dumps(message).encode() + b'\x03')

    def serve():
        buffer = b''
        while True:
            chunk = server.recv(65536)
            if not chunk:
                return
            buffer += chunk
            while b'\x03' in buffer:
                raw, buffer = buffer.split(b'\x03', 1)
                request = json.loads(raw)
                params = request.get('params', {})
                if request['method'] == 'gcode/subscribe_output':
                    template = params['response_template']
                    dispatch.register_output_handler(
                        lambda msg, template=template: send(dict(template, params={'response': msg})))
                    send({'id': request['id'], 'result': {}})
                    continue
                try:
                    dispatch.run_script(params['script'])
                except error_class as why:
                    send({'id': request['id'], 'error': {'error': 'WebRequestError', 'message': str(why)}})
                    continue
                send({'id': request['id'], 'result': {}})
    threading.Thread(target=serve, daemon=True).start()
    return Klippy('<contract>', timeout=5.0).connect(sock=client)


@pytest.mark.parametrize('source', fetched('gcode.py'))
@pytest.mark.parametrize('gconf, stealth', [(GCONF_STEALTH, True), (GCONF_SPREAD, False)])
def test_live_stealth_through_the_real_gcode_parser(source, gconf, stealth):
    require(source)
    module = load_gcode(source)
    printer, dispatch, ran = ready_dispatch(module, gconf)
    console = []
    dispatch.register_output_handler(console.append)
    kl = bridge(dispatch, module.CommandError)
    try:
        assert live_stealth(kl, 'stepper_x', tmc.DRIVERS['2209']) is stealth
        assert ran == ['DUMP_TMC STEPPER=stepper_x REGISTER=GCONF']
        assert not [line for line in console if line.startswith('!!')], console
        assert not printer.shutdowns
    finally:
        kl.close()


@pytest.mark.parametrize('source', fetched('webhooks.py'))
def test_gcode_output_through_the_real_api_server(source):
    """Our client against Klipper's own webhooks ClientConnection and GCodeHelper:
    subscription shape, console lines ahead of the script response, the '// ' prefix."""
    require(source)
    gcode_module = load_gcode(source)
    webhooks = load_webhooks(source, gcode_module)
    printer, dispatch, ran = ready_dispatch(gcode_module, GCONF_STEALTH)

    class Webhooks:
        def __init__(self):
            self.endpoints = {}

        def register_endpoint(self, path, callback):
            self.endpoints[path] = callback

        def get_callback(self, path):
            if path not in self.endpoints:
                raise webhooks.WebRequestError("No registered callback for path '%s'" % path)
            return self.endpoints[path]

    class Server:
        def __init__(self):
            self.printer, self.webhooks, self.reactor = printer, printer.objects['webhooks'], printer.reactor

        def pop_client(self, uid):
            pass

    printer.objects['webhooks'] = Webhooks()
    webhooks.GCodeHelper(printer)
    client, server_sock = socket.socketpair()
    connection = webhooks.ClientConnection(Server(), server_sock)

    def pump():
        while not connection.is_closed():
            readable, _, _ = select.select([server_sock], [], [], 0.2)
            if readable:
                connection.process_received(time.monotonic())
    threading.Thread(target=pump, daemon=True).start()
    kl = Klippy('<contract>', timeout=5.0).connect(sock=client)
    try:
        assert kl.gcode_output('DUMP_TMC STEPPER=stepper_x REGISTER=GCONF') == ['// ' + GCONF_STEALTH]
        assert live_stealth(kl, 'stepper_x', tmc.DRIVERS['2209']) is True
        # a Klipper error comes back as an error response at once, not as a timeout
        with pytest.raises(KlippyError, match='Malformed command'):
            kl.gcode('ECHO CHOPPER-7-BEGIN')
        assert not printer.shutdowns
    finally:
        kl.close()
        connection.close()


# every shape of line the tool and the tests send, plus the ones that must fail
PARSER_CASES = [
    *fence_markers('CHOPPER-4242-7'),
    'ECHO CHOPPER-7-BEGIN',
    'ECHO X=1 # a comment',
    'ECHO A="x',
    'RESPOND PREFIX="Chopper: " MSG="heating to 200C"',
    'DUMP_TMC STEPPER=stepper_x REGISTER=GCONF',
    'DUMP_TMC stepper_x',
    'SET_TMC_FIELD STEPPER=stepper_x FIELD=toff VALUE=3',
    'SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=1',
    'FORCE_MOVE STEPPER=stepper_x DISTANCE=20.400 VELOCITY=20.000 ACCEL=1000.000',
    'M117 Chopper: 3/40 tbl2_toff3',
    'G28 X Y',
]


@pytest.mark.parametrize('source', fetched('gcode.py'))
def test_the_fake_refuses_exactly_what_klipper_refuses(source):
    """tests/fake_klipper.py stands in for Klipper everywhere else: hold its rule to
    the real parser of every fetched release."""
    require(source)
    module = load_gcode(source)
    printer, dispatch, _ = ready_dispatch(module, GCONF_STEALTH)
    for name in ('RESPOND', 'SET_TMC_FIELD', 'FORCE_MOVE', 'M117', 'G28'):
        dispatch.register_command(name, lambda gcmd: None)
    for line in PARSER_CASES:
        try:
            dispatch.run_script(line)
            refused = False
        except module.CommandError:
            refused = True
        assert refused == fake_klipper.malformed(line), (source, line)
