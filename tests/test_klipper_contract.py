"""Contract with the REAL Klipper/Kalico G-code parser and API server. The fakes in the
other tests only repeat our own assumptions: a bare-word ECHO fence passed them and
was a 'Malformed command' on every printer. The GPL sources are fetched, never
committed (tests/fetch_klipper_sources.sh); CI sets CHOPPER_CONTRACT=1 so a missing
download fails instead of skipping."""
import ast
import configparser
import fnmatch
import importlib.util
import json
import math
import os
import re
import select
import shutil
import socket
import sys
import tempfile
import threading
import time
import types

import pytest

import fake_klipper
from klipper_config import CFG, named, read_like_klipper, selfcheck, settings_of
from chopper_autotune import collect, tmc
from chopper_autotune.belts import CAPTURE, sweep_chip, sweep_command
from chopper_autotune.collect import ACCEL_SECTIONS, live_stealth, resolve_accel_chip
from chopper_autotune.klippy import Klippy, KlippyError, fence_markers

# old Klipper releases carry regex strings Python warns about while compiling them
# (DeprecationWarning up to 3.11, SyntaxWarning from 3.12)
pytestmark = [pytest.mark.filterwarnings('ignore:invalid escape sequence:DeprecationWarning'),
              pytest.mark.filterwarnings('ignore:invalid escape sequence:SyntaxWarning')]

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


def load_klippy(source: str, filename: str):
    path = os.path.join(SRC, source, filename + '.py')
    tag = source.replace('.', '_').replace('-', '_')
    name = 'contract_%s_%s' % (tag, filename)
    if source.startswith('kalico'):
        # Kalico's klippy files are package modules ('from . import mathutil'); the real
        # mathutil pulls in its logging machinery, the parser only needs safe_float, and
        # reading a config never asks the danger options the config reader imports
        package = types.ModuleType('contract_%s' % tag)
        package.__path__ = [os.path.dirname(path)]
        mathutil = types.ModuleType(package.__name__ + '.mathutil')
        mathutil.safe_float = safe_float
        extras = types.ModuleType(package.__name__ + '.extras')
        extras.__path__ = []
        danger_options = types.ModuleType(extras.__name__ + '.danger_options')
        danger_options.get_danger_options = None
        package.mathutil, package.extras, extras.danger_options = mathutil, extras, danger_options
        for stub in (package, mathutil, extras, danger_options):
            sys.modules[stub.__name__] = stub
        name = package.__name__ + '.' + filename
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_gcode(source: str):
    return load_klippy(source, 'gcode')


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
    NEVER = 9999999999999999.

    def __init__(self):
        self.timers = []

    def register_timer(self, callback, waketime=None):
        self.timers.append(callback)
        return callback

    def unregister_timer(self, handle):
        if handle in self.timers:
            self.timers.remove(handle)

    def fire_timers(self):
        for timer in list(self.timers):
            timer(time.monotonic())

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


def api_client(printer, webhooks):
    """Our client on one real webhooks.py ClientConnection to the printer's endpoints."""
    class Server:
        def __init__(self):
            self.printer, self.webhooks, self.reactor = printer, printer.objects['webhooks'], printer.reactor

        def pop_client(self, uid):
            pass

    client, server_sock = socket.socketpair()
    connection = webhooks.ClientConnection(Server(), server_sock)
    # Klipper sends from its one reactor thread; here the pump answers requests while
    # the test thread fires the stream timers
    lock, send = threading.Lock(), connection.send

    def locked_send(data):
        with lock:
            send(data)
    connection.send = locked_send

    def pump():
        while not connection.is_closed():
            try:
                readable, _, _ = select.select([server_sock], [], [], 0.2)
            except (OSError, ValueError):
                return                              # closed under us by the test's finally
            if readable:
                connection.process_received(time.monotonic())
    threading.Thread(target=pump, daemon=True).start()
    return Klippy('<contract>', timeout=5.0).connect(sock=client), connection


# v0.10's API server is Python 2 code, Kalico's a package module: the parser contract
# above covers both
@pytest.mark.parametrize('source', [source for source in fetched('webhooks.py') if source is None
                                    or not (source.endswith('v0.10.0') or source.startswith('kalico'))])
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

    printer.objects['webhooks'] = Webhooks()
    webhooks.GCodeHelper(printer)
    kl, connection = api_client(printer, webhooks)
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


@pytest.mark.parametrize('source', fetched('bulk_sensor.py'))
def test_every_streamed_sample_arrives_once(source):
    """Klipper's stream helper (every accelerometer since v0.12.0-53, Kalico too) adds a
    client per dump request and sends each batch to all of them, until the connection
    closes. Replays CHOPPER_TUNE AXIS=xy without SPEED on a printer with accel_chip_x
    and accel_chip_y: scan and descent of each motor subscribe on one connection."""
    require(source)
    # the API server of the Klipper whose stream helper is fetched (master)
    api = next((name for name in fetched('bulk_sensor.py') if name and name.startswith('klipper')), None)
    require(api)
    gcode_module = load_gcode(api)
    webhooks = load_webhooks(api, gcode_module)
    bulk_sensor = load_klippy(source, 'bulk_sensor')
    printer = Printer()
    printer.command_error = gcode_module.CommandError
    printer.objects['webhooks'] = webhooks.WebHooks(printer)
    pending = {}
    for sensor in ('hotend', 'bed'):
        helper = bulk_sensor.BatchBulkHelper(
            printer, lambda eventtime, sensor=sensor: pending.pop(sensor, None))
        helper.add_mux_endpoint('adxl345/dump_adxl345', 'sensor', sensor,
                                {'header': ('time', 'x_acceleration', 'y_acceleration', 'z_acceleration')})
    kl, connection = api_client(printer, webhooks)

    def stream(start):
        """One batch from each chip, told apart by the value; then a later one past it."""
        batches = {sensor: [[start + i / 3200.0, value, value, value] for i in range(64)]
                   for sensor, value in (('hotend', 1.0), ('bed', 2.0))}
        for data in (batches, {sensor: [[start + 1.0, 0.0, 0.0, 0.0]] for sensor in batches}):
            pending.update((sensor, {'data': samples, 'errors': 0, 'overflows': 0})
                           for sensor, samples in data.items())
            printer.reactor.fire_timers()
        kl.wait_for_sample(start + 1.0)             # every copy of the first batch is in
        return batches

    try:
        for start, chip in enumerate(['adxl345 hotend', 'adxl345 hotend',    # motor A: scan, descent
                                      'adxl345 bed', 'adxl345 bed']):        # motor B: scan, descent
            kl.subscribe_accel(chip)
            batches = stream(10.0 * start)
            assert kl.samples_between(10.0 * start, 10.0 * start + 0.5) == batches[chip.split()[-1]], \
                (source, start, chip)
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
    'SET_STEPPER_ENABLE STEPPER="extruder_stepper belted" ENABLE=0',
    'SET_STEPPER_ENABLE STEPPER=extruder_stepper belted ENABLE=0',
    'SET_KINEMATIC_POSITION SET_HOMED= CLEAR_HOMED=XY',
    'FORCE_MOVE STEPPER=stepper_x DISTANCE=20.400 VELOCITY=20.000 ACCEL=1000.000',
    'M117 Chopper: 3/40 tbl2_toff3',
    'G28 X Y',
    'TEST_RESONANCES AXIS=1,1 OUTPUT=raw_data NAME=beltA CHIPS="adxl345 hotend" FREQ_START=30',
]


@pytest.mark.parametrize('source', fetched('gcode.py'))
def test_the_fake_refuses_exactly_what_klipper_refuses(source):
    """tests/fake_klipper.py stands in for Klipper everywhere else: hold its rule to
    the real parser of every fetched release."""
    require(source)
    module = load_gcode(source)
    printer, dispatch, _ = ready_dispatch(module, GCONF_STEALTH)
    for name in ('RESPOND', 'SET_TMC_FIELD', 'FORCE_MOVE', 'M117', 'G28', 'SET_KINEMATIC_POSITION',
                 'TEST_RESONANCES'):
        dispatch.register_command(name, lambda gcmd: None)
    dispatch.register_mux_command('SET_STEPPER_ENABLE', 'STEPPER', 'extruder_stepper belted',
                                  lambda gcmd: None)
    for line in PARSER_CASES:
        try:
            dispatch.run_script(line)
            refused = False
        except module.CommandError:
            refused = True
        assert refused == fake_klipper.malformed(line), (source, line)


def read_main_config(source: str, path: str, monkeypatch):
    """The config as this release's own reader builds it from printer.cfg, includes and all."""
    # v0.11/v0.12 call readfp, gone since Python 3.12 (their printers run 3.9 to 3.11)
    monkeypatch.setattr(configparser.RawConfigParser, 'readfp',
                        configparser.RawConfigParser.read_file, raising=False)
    module = load_klippy(source, 'configfile')
    with open(path) as main:
        data = main.read()
    if hasattr(module, 'ConfigFileReader'):                 # v0.13 and later
        return module.ConfigFileReader().build_fileconfig_with_includes(data, path)
    config = module.PrinterConfig.__new__(module.PrinterConfig)
    config.printer = None
    return config._build_config_wrapper(data, path).fileconfig


def printer_cfg(tmp_path, body: str) -> str:
    """printer.cfg as install.sh leaves it: our include on the first line."""
    (tmp_path / 'chopper_autotune.cfg').symlink_to(CFG)
    (tmp_path / 'printer.cfg').write_text('[include chopper_autotune.cfg]\n' + body)
    return str(tmp_path / 'printer.cfg')


OTHER_TOOLS = {
    # chopper-resonance-tuner: its own macro of our name
    'CHOPPER_TUNE': ('[gcode_macro CHOPPER_TUNE]\ngcode:\n    _chop_workflow\n',
                     ('gcode_macro CHOPPER_TUNE', 'gcode', '_chop_workflow')),
    # gschpoozi: a macro that looks like ours, and a shell command of our name
    'CHOPPER_ANALYZE': ('[gcode_shell_command chopper_analyze]\n'
                        'command: python3 ~/gschpoozi/scripts/tools/chopper_analyze.py\n'
                        '[gcode_macro CHOPPER_ANALYZE]\n'
                        'gcode:\n    RUN_SHELL_COMMAND CMD=chopper_analyze PARAMS="{rawparams}"\n',
                        ('gcode_shell_command chopper_analyze', 'command', 'gschpoozi')),
}


@pytest.mark.parametrize('source', fetched('configfile.py'))
@pytest.mark.parametrize('name', sorted(OTHER_TOOLS))
def test_a_name_defined_again_later_replaces_ours_without_an_error(source, name, tmp_path, monkeypatch):
    # #132: another tuner's installer also puts its include on the first line of
    # printer.cfg, so a tuner installed before ours ends up below it and wins
    require(source)
    text, (section, option, theirs) = OTHER_TOOLS[name]
    (tmp_path / 'other.cfg').write_text(text)
    fileconfig = read_main_config(source, printer_cfg(tmp_path, '[include other.cfg]\n'), monkeypatch)
    assert theirs in fileconfig.get(section, option)
    assert named(selfcheck(settings_of(fileconfig))) == [name]


@pytest.mark.parametrize('source', fetched('configfile.py'))
@pytest.mark.parametrize('yours, enabled', [('', 'True'),
                                            ('enable_force_move: False\n', 'False'),
                                            ('enable_force_move: True\n', 'True')])
def test_a_force_move_of_your_own_merges_with_ours(source, tmp_path, monkeypatch, yours, enabled):
    require(source)
    fileconfig = read_main_config(source, printer_cfg(tmp_path, '[force_move]\n' + yours), monkeypatch)
    assert fileconfig.get('force_move', 'enable_force_move') == enabled
    assert selfcheck(settings_of(fileconfig)) is None


@pytest.mark.parametrize('source', fetched('gcode.py'))
def test_klipper_accepts_our_macro_names(source):
    require(source)
    _, dispatch, _ = ready_dispatch(load_gcode(source), GCONF_STEALTH)
    for section in read_like_klipper(CFG).sections():
        if section.startswith('gcode_macro '):
            dispatch.register_command(section.split()[1].upper(), lambda gcmd: None)


@pytest.mark.parametrize('source', fetched('lis2dw.py'))
def test_every_accelerometer_streams_where_we_subscribe(source):
    """subscribe_accel builds the endpoint from the section type: hold it to the one the
    real module registers (a [lis3dh] section is served by lis2dw.py)."""
    require(source)
    root = os.path.join(SRC, source)
    checked = []
    for section in ACCEL_SECTIONS:
        path = os.path.join(root, section + '.py')
        if not os.path.exists(path):
            continue                                    # Kalico has no bmi160
        with open(path) as module:
            text = module.read()
        delegate = re.search(r'from \. import (\w+)', text)
        if 'add_mux_endpoint' not in text and delegate:
            with open(os.path.join(root, delegate.group(1) + '.py')) as module:
                text = module.read()
        registered = re.findall(r'add_mux_endpoint\(\s*"(\w+/dump_\w+)",\s*"sensor"', text)
        calls = []
        kl = Klippy('<contract>')
        kl.request = lambda method, params: calls.append(method)
        kl.subscribe_accel(section)
        assert calls == registered, (source, section)
        checked.append(section)
    assert {'adxl345', 'lis2dw', 'lis3dh', 'mpu9250', 'icm20948'} <= set(checked)
    assert 'bmi160' in checked or source.startswith('kalico')      # Klipper master has it


def load_extra(source: str, filename: str):
    """A klippy/extras module of the release, inside a stub package: resonance_tester's
    shaper_calibrate serves only OUTPUT=resonances, which the sweep never asks for."""
    tag = source.replace('.', '_').replace('-', '_')
    package = types.ModuleType('contract_%s_extras' % tag)
    package.__path__ = [os.path.join(SRC, source)]
    package.shaper_calibrate = types.ModuleType(package.__name__ + '.shaper_calibrate')
    for module in (package, package.shaper_calibrate):
        sys.modules[module.__name__] = module
    name = package.__name__ + '.' + filename
    spec = importlib.util.spec_from_file_location(name, os.path.join(SRC, source, filename + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def real_lookup_object(source: str):
    """Printer.lookup_object of the release itself (Kalico keeps Printer in printer.py):
    an unknown name raises the config error, which is no G-code error."""
    filename = 'printer.py' if source.startswith('kalico') else 'klippy.py'
    with open(os.path.join(SRC, source, filename)) as module:
        tree = ast.parse(module.read())
    printer = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'Printer')
    method = next(node for node in printer.body
                  if isinstance(node, ast.FunctionDef) and node.name == 'lookup_object')
    namespace = {'configfile': types.SimpleNamespace(sentinel=object())}
    exec(compile(ast.Module(body=[method], type_ignores=[]), filename, 'exec'), namespace)
    return namespace['lookup_object']


class Accelerometer:
    """What resonance_tester asks of a chip: a client per test that writes the raw file.
    Every chip module names itself by the last word of its section."""

    def __init__(self, section: str, written: list):
        self.section, self.name, self.written = section, section.split()[-1], written

    def start_internal_client(self):
        chip = self

        class Client:
            def finish_measurements(self):
                pass

            def has_valid_samples(self):
                return True

            def write_to_file(self, filename):
                chip.written.append((chip.section, filename))
        return Client()


class Section:
    """[resonance_tester] as the config reader serves it: an option not written takes the
    module's default, a required one is a config error."""
    error = configparser.Error
    required = object()

    def __init__(self, printer, options: dict):
        self.printer, self.options = printer, options

    def get_printer(self):
        return self.printer

    def get(self, option, default=required):
        if option in self.options:
            return self.options[option]
        if default is self.required:
            raise self.error("Option '%s' in section 'resonance_tester' must be specified" % option)
        return default

    def getfloat(self, option, default=required, **limits):
        value = self.get(option, default)
        return value if value is None else float(value)

    def getlists(self, option, seps, parser, count):
        return [tuple(parser(v) for v in point.split(',')) for point in self.get(option).split('\n')]


class Toolhead:
    def manual_move(self, coord, speed):
        pass

    def wait_moves(self):
        pass

    def dwell(self, delay):
        pass


def sweep_on(source: str, tester: dict, sections: 'list[str]', line: str, monkeypatch):
    """`line` through the release's own G-code dispatcher, TEST_RESONANCES and
    Printer.lookup_object, with an accelerometer object for each config section, the
    motion left out. Returns the printer, the raw files written as (chip, path), and the
    error the script raised."""
    module = load_extra(source, 'resonance_tester')
    printer, dispatch, _ = ready_dispatch(load_gcode(source), GCONF_STEALTH)
    printer.config_error = configparser.Error               # klippy: configfile.error
    printer.lookup_object = types.MethodType(real_lookup_object(source), printer)
    written = []
    for section in sections:
        printer.objects[section] = Accelerometer(section, written)
    printer.objects['toolhead'] = Toolhead()
    resonance = module.ResonanceTester(Section(printer, dict(tester, probe_points='100,100,20')))
    # the moves: the pulse test's up to v0.12, the executor's from v0.13 and in Kalico
    motion = getattr(resonance, 'executor', None) or resonance.test
    monkeypatch.setattr(motion, 'run_test', lambda *args, **kwargs: None)
    monkeypatch.setattr(tempfile, 'tempdir', '/tmp')        # Kalico writes to gettempdir()
    printer.send_event('klippy:connect')
    try:
        dispatch.run_script(line)
    except Exception as why:
        return printer, written, why
    return printer, written, None


def clean_capture(source, tester, sections, chip, named, monkeypatch) -> bool:
    """The sweep command, CHIPS= naming `named` (None: no CHIPS=), runs without an error
    or a shutdown, and exactly one file is written: `chip`'s, where the sweep looks."""
    line = sweep_command('1,1', 'A', named, (30, 200), 2)
    printer, written, error = sweep_on(source, tester, sections, line, monkeypatch)
    return (error is None and not printer.shutdowns and [chip for chip, _ in written] == [chip]
            and fnmatch.fnmatch(written[0][1], CAPTURE % 'A'))


# [resonance_tester] options, and the accelerometer sections the config has
SWEEP_TESTERS = [
    ({'accel_chip': 'adxl345'}, ['adxl345']),
    ({'accel_chip': 'adxl345 hotend'}, ['adxl345 hotend']),
    ({'accel_chip': 'lis2dw'}, ['lis2dw']),
    ({'accel_chip': 'lis2dw Head'}, ['lis2dw Head']),
    ({'accel_chip': 'beacon'}, ['beacon']),
    ({'accel_chip_x': 'adxl345 hotend', 'accel_chip_y': 'lis2dw bed'}, ['adxl345 hotend', 'lis2dw bed']),
    ({'accel_chip_x': 'lis2dw hotend', 'accel_chip_y': 'adxl345 bed'}, ['lis2dw hotend', 'adxl345 bed']),
    ({'accel_chip_x': 'lis2dw hotend', 'accel_chip_y': 'lis2dw hotend'}, ['lis2dw hotend']),
    # Kalico only: every accel_chips entry measures, accel_chip_x names the tool's chip,
    # and Kalico never looks that name up at start
    ({'accel_chips': 'adxl345 bed, lis2dw hotend', 'accel_chip_x': 'lis2dw hotend',
      'accel_chip_y': 'lis2dw hotend'}, ['adxl345 bed', 'lis2dw hotend']),
    ({'accel_chips': 'adxl345 bed, lis2dw hotend', 'accel_chip_x': 'adxl345 Head',
      'accel_chip_y': 'adxl345 Head'}, ['adxl345 bed', 'lis2dw hotend', 'adxl345 Head']),
    ({'accel_chips': 'adxl345 bed, lis2dw hotend', 'accel_chip_x': 'adxl345 head',
      'accel_chip_y': 'adxl345 head'}, ['adxl345 bed', 'lis2dw hotend', 'adxl345 Head']),
    ({'accel_chips': 'adxl345 bed, lis2dw hotend', 'accel_chip_x': 'lis2dw',
      'accel_chip_y': 'lis2dw'}, ['adxl345 bed', 'lis2dw hotend']),
]


def running_klipper(source: str, sections: 'list[str]', root, monkeypatch, started: float):
    """Our client's view of a Klipper process of this release that started at `started`:
    info as its webhooks.py answers it (process_id since v0.12), settings keys
    lower-cased as its config reader keeps them, section names as written."""
    with open(os.path.join(SRC, source, 'webhooks.py')) as module:
        reports_process = re.search(r'[\'"]process_id[\'"]', module.read()) is not None
    monkeypatch.setattr(collect, '_KLIPPER_EXTRAS', {})
    monkeypatch.setattr(collect, 'process_start', lambda pid: None if pid is None else started)
    info = dict({'klipper_path': str(root)}, **({'process_id': 1} if reports_process else {}))
    return types.SimpleNamespace(info=lambda: info, config_sections=lambda: list(sections))


def installed(root, source: str) -> str:
    """klippy/extras/resonance_tester.py of `source` on disk under `root`."""
    (root / 'klippy' / 'extras').mkdir(parents=True, exist_ok=True)
    return shutil.copy(os.path.join(SRC, source, 'resonance_tester.py'), root / 'klippy' / 'extras')


def sweep_settings(tester: dict, sections: 'list[str]') -> dict:
    return dict({section.lower(): {} for section in sections},
                resonance_tester=dict(tester, probe_points=[[100.0, 100.0, 20.0]]))


@pytest.mark.parametrize('source, tester, sections', [
    (source, tester, sections) for source in fetched('resonance_tester.py')
    for tester, sections in SWEEP_TESTERS
    if source is None or source.startswith('kalico') or 'accel_chips' not in tester])
def test_the_belt_sweep_measures_its_chip_and_never_shuts_klipper_down(source, tester, sections,
                                                                       tmp_path, monkeypatch):
    require(source)
    settings = sweep_settings(tester, sections)
    chip = resolve_accel_chip(settings, 'x')
    installed(tmp_path, source)
    kl = running_klipper(source, sections, tmp_path, monkeypatch, started=time.time() + 60)
    try:
        named_chip = sweep_chip(kl, settings, chip)
    except SystemExit:
        # refused only where no command measures the chip alone and safely
        assert not clean_capture(source, tester, sections, chip, chip, monkeypatch)
        assert not clean_capture(source, tester, sections, chip, None, monkeypatch)
        return
    assert clean_capture(source, tester, sections, chip, named_chip, monkeypatch)


@pytest.mark.parametrize('running', [source for source in fetched('resonance_tester.py')
                                     if source is None or source.startswith('klipper-v0.1')])
@pytest.mark.parametrize('tester, sections', SWEEP_TESTERS[:8])
def test_code_pulled_after_klipper_started_never_shuts_it_down(running, tester, sections, tmp_path,
                                                               monkeypatch):
    # RESTART and FIRMWARE_RESTART keep the imported modules: after a git pull to master
    # without a service restart, the running release still parses CHIPS= its own way
    require(running)
    newest = [source for source in fetched('resonance_tester.py') if source.startswith('klipper-ce')]
    require(newest[0] if newest else None)
    settings = sweep_settings(tester, sections)
    chip = resolve_accel_chip(settings, 'x')
    installed(tmp_path, newest[0])
    kl = running_klipper(running, sections, tmp_path, monkeypatch, started=time.time() - 60)
    try:
        named_chip = sweep_chip(kl, settings, chip)
    except SystemExit:
        return                                          # refused before any G-code
    line = sweep_command('1,1', 'A', named_chip, (30, 200), 2)
    printer, _, error = sweep_on(running, tester, sections, line, monkeypatch)
    assert error is None and not printer.shutdowns, (line, error)


@pytest.mark.parametrize('source', fetched('resonance_tester.py'))
def test_the_old_sweep_shut_v0_11_and_v0_12_down(source, monkeypatch):
    """The harness reproduces the reported failure: CHIPS= with a chip that has no
    'adxl345' in its name, as the sweep sent it everywhere before."""
    require(source)
    line = ('TEST_RESONANCES AXIS=1,1 OUTPUT=raw_data NAME=beltA CHIPS="lis2dw" '
            'FREQ_START=30 FREQ_END=200 HZ_PER_SEC=2')
    printer, _, error = sweep_on(source, {'accel_chip': 'lis2dw'}, ['lis2dw'], line, monkeypatch)
    old = source in ('klipper-v0.11.0', 'klipper-v0.12.0')
    assert printer.shutdowns == (['Internal error on command:"TEST_RESONANCES"'] if old else [])
    assert isinstance(error, configparser.Error) is old
