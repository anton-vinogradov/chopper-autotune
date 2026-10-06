"""Contract with the REAL Klipper/Kalico G-code parser and API server. The fakes in the
other tests only repeat our own assumptions: a bare-word ECHO fence passed them and
was a 'Malformed command' on every printer. The GPL sources are fetched, never
committed (tests/fetch_klipper_sources.sh); CI sets CHOPPER_CONTRACT=1 so a missing
download fails instead of skipping."""
import ast
import collections
import configparser
import contextlib
import copy
import fnmatch
import importlib.util
import json
import math
import os
import re
import select
import shutil
import socket
import struct
import subprocess
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
from chopper_autotune.current import BELT_SPEEDS, belt_cap, shaper_freqs, stress_vector, velocity_caps
from chopper_autotune.envelope import STRESS_REPS
from chopper_autotune.collect import ACCEL_SECTIONS, accel_command_chip, live_stealth, resolve_accel_chip
from chopper_autotune.klippy import Klippy, KlippyError, fence_markers

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

    def assert_no_pause(self):                      # master's template status reads
        return contextlib.nullcontext()

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


# Kalico's API server is a package module: the parser contract above covers it
@pytest.mark.parametrize('source', [source for source in fetched('webhooks.py')
                                     if source is None or not source.startswith('kalico')])
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
    """Klipper's stream helper (every accelerometer, Kalico too) adds a
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
    'RESPOND PREFIX="Chopper:" MSG="heating to 200C"',
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
    module = load_klippy(source, 'configfile')
    with open(path) as main:
        data = main.read()
    if hasattr(module, 'ConfigFileReader'):                 # Klipper; Kalico reads in PrinterConfig
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


@pytest.mark.version_gate
@pytest.mark.parametrize('source', fetched('force_move.py'))
def test_every_supported_klipper_passes_the_version_check(source, tmp_path, monkeypatch):
    require(source)
    (tmp_path / 'klippy' / 'extras').mkdir(parents=True)
    shutil.copy(os.path.join(SRC, source, 'force_move.py'), str(tmp_path / 'klippy' / 'extras'))
    monkeypatch.setattr(collect, '_KLIPPER_EXTRAS', {})
    monkeypatch.setattr(collect, 'process_start', lambda pid: float('inf'))
    collect.require_current_klipper(types.SimpleNamespace(
        info=lambda: {'klipper_path': str(tmp_path), 'process_id': 1}))


@pytest.mark.parametrize('source', fetched('tmc2240.py'))
def test_the_autotune_advice_names_only_options_the_tmc2240_section_takes(source):
    # a [tmc2240] line naming an option Klipper does not read stops it at start; it
    # records every option it reads in the settings, defaults too (set_config_field)
    require(source)
    with open(os.path.join(SRC, source, 'tmc2240.py')) as module:
        read = re.findall(r'set_config_field\(\s*config,\s*["\'](\w+)["\']', module.read())
    assert {'sgt', 'slope_control'} <= set(read), source
    settings = {'tmc2240 stepper_x': {'driver_' + field: 0 for field in read},
                'autotune_tmc stepper_x': {'sgt': 1, 'sg4_thrs': 60}}
    lines = collect.autotune_carry_over(settings, '2240', 'stepper_x')
    assert lines and {line.split(':')[0] for line in lines} <= {'driver_' + field.upper() for field in read}
    assert any(line.startswith('driver_SG4_THRS') for line in lines) is ('sg4_thrs' in read)


@pytest.mark.parametrize('source', fetched('configfile.py'))
def test_a_single_accelerometer_keeps_its_name_as_written(source, tmp_path, monkeypatch):
    # settings has the section names lower-cased (access tracking), while a chip registers
    # ACCELEROMETER_MEASURE and its stream under its section name as written
    require(source)
    fileconfig = read_main_config(source, printer_cfg(tmp_path, '[adxl345 Hotend]\ncs_pin: PA4\n'),
                                  monkeypatch)
    tracking = {}
    load_klippy(source, 'configfile').ConfigWrapper(None, fileconfig, tracking, 'printer') \
        .getsection('adxl345 Hotend').get('cs_pin')
    settings = {}
    for (section, option), value in tracking.items():      # as the configfile status builds it
        settings.setdefault(section, {})[option] = value
    assert list(settings) == ['adxl345 hotend']
    chip = resolve_accel_chip(settings, 'x', fileconfig.sections)
    assert chip == 'adxl345 Hotend' and accel_command_chip(settings, chip) == 'Hotend'


# a TMPDIR of klippy's service: Kalico writes the raw files there, and the tool that
# RUN_SHELL_COMMAND starts inherits it
KLIPPY_TMPDIR = '/srv/klippy-tmp'


def where_the_tool_looks(path: str, pattern: str) -> bool:
    return any(fnmatch.fnmatch(os.path.realpath(path), os.path.join(d, pattern))
               for d in collect.capture_dirs())


def accel_dispatch(source: str, chip):
    """The release's G-code dispatcher with the accelerometer commands of `chip`, an
    'adxl345 Hotend' section."""
    tag = source.replace('.', '_').replace('-', '_')
    package = types.ModuleType('contract_%s_accel' % tag)
    package.__path__ = [os.path.join(SRC, source)]
    stubs = [package]
    for stub in ('bus', 'bulk_sensor'):                     # used by the chip, not the commands
        module = types.ModuleType(package.__name__ + '.' + stub)
        setattr(package, stub, module)
        stubs.append(module)
    for module in stubs:
        sys.modules[module.__name__] = module
    name = package.__name__ + '.adxl345'
    spec = importlib.util.spec_from_file_location(name, os.path.join(SRC, source, 'adxl345.py'))
    adxl345 = importlib.util.module_from_spec(spec)
    sys.modules[name] = adxl345
    spec.loader.exec_module(adxl345)
    printer, dispatch, _ = ready_dispatch(load_gcode(source), GCONF_STEALTH)
    adxl345.AccelCommandHelper(types.SimpleNamespace(
        get_printer=lambda: printer, get_name=lambda: 'adxl345 Hotend', error=configparser.Error), chip)
    return dispatch


@pytest.mark.parametrize('source', fetched('adxl345.py'))
def test_accelerometer_measure_takes_the_chip_name_as_written(source):
    require(source)
    dispatch = accel_dispatch(source, object())
    assert list(dispatch.mux_commands['ACCELEROMETER_MEASURE'][1]) == [
        accel_command_chip({}, 'adxl345 Hotend')]


@pytest.mark.parametrize('source', fetched('adxl345.py'))
def test_the_csv_capture_looks_where_accelerometer_measure_writes(source, monkeypatch):
    require(source)
    monkeypatch.setattr(tempfile, 'tempdir', KLIPPY_TMPDIR)
    written = []
    dispatch = accel_dispatch(source, Accelerometer('adxl345 Hotend', written))
    console = []
    dispatch.register_output_handler(console.append)
    measure = 'ACCELEROMETER_MEASURE CHIP=%s NAME=v060' % accel_command_chip({}, 'adxl345 Hotend')
    dispatch.run_script('\n'.join([measure, measure]))       # start, then stop and write
    assert len(written) == 1 and where_the_tool_looks(written[0][1], '*-v060.csv')
    # the console names the file: found also by a tool that does not share klippy's TMPDIR
    assert collect.written_files(console) == [written[0][1]]


@pytest.mark.parametrize('source', fetched('gcode.py'))
def test_the_ranking_table_keeps_its_columns_in_the_console(source, capsys):
    # CHOPPER_ANALYZE runs in the foreground: its output reaches respond_info, which strips
    # the leading spaces of every line, and the console shows it in a monospace font
    from chopper_autotune.analyze import print_table, rank
    require(source)
    ranked = rank([{'chopper': tmc.Chopper(tbl, toff, 4, 6), 'magnitude': 100.0 * toff + tbl,
                    'spread': 1.0, 'n': 2} for tbl in range(4) for toff in range(2, 5)],
                  tmc.DRIVERS['2209'], tmc.Hearing())
    print_table(ranked, 12)
    dispatch = load_gcode(source).GCodeDispatch(Printer())
    console = []
    dispatch.register_output_handler(console.append)
    dispatch.respond_info(capsys.readouterr().out)
    lines = [line[len('// '):] for message in console for line in message.split('\n')]

    def column_edges(line):
        # rank flush left at 0, the other ten columns flush right
        return [match.end() for match in re.finditer(r'\S+', line)][1:11]
    header = column_edges(lines[0])
    assert [column_edges(line) for line in lines[1:]] == [header] * 12


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
    printer.console = []
    dispatch.register_output_handler(printer.console.append)
    written = []
    for section in sections:
        printer.objects[section] = Accelerometer(section, written)
    printer.objects['toolhead'] = Toolhead()
    resonance = module.ResonanceTester(Section(printer, dict(tester, probe_points='100,100,20')))
    monkeypatch.setattr(resonance.executor, 'run_test', lambda *args, **kwargs: None)   # the moves
    monkeypatch.setattr(tempfile, 'tempdir', KLIPPY_TMPDIR)
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
            and where_the_tool_looks(written[0][1], CAPTURE % 'A')
            and collect.written_files(printer.console) == [written[0][1]])


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


def sweep_settings(tester: dict, sections: 'list[str]') -> dict:
    return dict({section.lower(): {} for section in sections},
                resonance_tester=dict(tester, probe_points=[[100.0, 100.0, 20.0]]))


@pytest.mark.parametrize('source, tester, sections', [
    (source, tester, sections) for source in fetched('resonance_tester.py')
    for tester, sections in SWEEP_TESTERS
    if source is None or source.startswith('kalico') or 'accel_chips' not in tester])
def test_the_belt_sweep_measures_its_chip_and_never_shuts_klipper_down(source, tester, sections,
                                                                       monkeypatch):
    require(source)
    settings = sweep_settings(tester, sections)
    chip = resolve_accel_chip(settings, 'x')
    kl = types.SimpleNamespace(config_sections=lambda: list(sections))
    try:
        named_chip = sweep_chip(kl, settings, chip)
    except SystemExit:
        # refused only where no command measures the chip alone and safely
        assert not clean_capture(source, tester, sections, chip, chip, monkeypatch)
        assert not clean_capture(source, tester, sections, chip, None, monkeypatch)
        return
    assert clean_capture(source, tester, sections, chip, named_chip, monkeypatch)


# what a Beacon RevH reports in klippy.log (firmware 2.1.0): scales in mg per count,
# enumerated by the MCU with 16g, the default, as id 0
BEACON_CONSTANTS = {'BEACON_ACCEL_BITS': 12, 'BEACON_ACCEL_SCALE_16G': '7.81',
                    'BEACON_ACCEL_SCALE_8G': '3.91', 'BEACON_ACCEL_SCALE_4G': '1.95',
                    'BEACON_ACCEL_SCALE_2G': '0.98'}
BEACON_SCALES = {'16g': 0, '8g': 1, '4g': 2, '2g': 3}
BEACON_CLOCK = 32e6
BEACON_TICKS = 9608                 # clock ticks per sample: ~3.33 kHz


def load_beacon(klipper: str, monkeypatch):
    """beacon.py as Klipper loads it from klippy/extras, beside the release's own
    adxl345.py (the ACCELEROMETER_* commands); the klippy modules only its probe side
    uses are stubs."""
    source = fetched('beacon.py')[-1]
    require(source)
    package = types.ModuleType('contract_beacon_%s' % klipper.replace('.', '_').replace('-', '_'))
    package.__path__ = []
    monkeypatch.setitem(sys.modules, package.__name__, package)
    for name in ('manual_probe', 'probe', 'bed_mesh', 'thermistor', 'homing', 'bus', 'bulk_sensor'):
        stub = types.ModuleType(package.__name__ + '.' + name)
        setattr(package, name, stub)
        monkeypatch.setitem(sys.modules, stub.__name__, stub)
    package.homing.HomingMove = object
    for name, attributes in (('chelper', {}), ('pins', {'error': Exception}),
                             ('msgproto', {'error': Exception}),
                             ('mcu', {'MCU': object, 'MCU_trsync': object}),
                             ('clocksync', {'SecondarySync': object}),
                             ('configfile', {'error': Printer.config_error})):
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(**attributes))
    for name, path in (('adxl345', os.path.join(SRC, klipper, 'adxl345.py')),
                       ('beacon', os.path.join(SRC, source, 'beacon.py'))):
        spec = importlib.util.spec_from_file_location(package.__name__ + '.' + name, path)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, spec.name, module)
        setattr(package, name, module)
        spec.loader.exec_module(module)
    return package.beacon


class Options(dict):
    """A config section holding just the options given."""
    error = Printer.config_error

    def getlist(self, option, default, count=None):
        return self[option].split(',') if option in self else default


class BeaconMcu:
    def __init__(self):
        self.sent = []

    def lookup_command(self, msgformat, cq=None):
        return types.SimpleNamespace(send=self.sent.append)

    def get_enumerations(self):
        return {'beacon_accel_scales': BEACON_SCALES}

    def clock32_to_clock64(self, clock):
        return clock

    def clock_to_print_time(self, clock):
        return clock / BEACON_CLOCK


class Beacon:
    """The parts of beacon.py's BeaconProbe its accelerometer helper uses, with the
    probe's real clock conversion."""

    def __init__(self, module, printer, tracker, name=None, **options):
        self.printer, self.cmd_queue, self._mcu = printer, None, BeaconMcu()
        self.id = module.BeaconId(name, tracker)
        self.responses = {}
        self._clock32_to_time = types.MethodType(module.BeaconProbe._clock32_to_time, self)
        self.accel = module.BeaconAccelHelper(
            self, module.BeaconAccelConfig(Options(options), self.id), BEACON_CONSTANTS)

    def compat_mcu_register_response(self, callback, msgformat, oid=None):
        self.responses[msgformat.split()[0]] = callback

    def report(self, clock: int, counts: 'list[tuple[int, int, int]]'):
        """One beacon_accel_data message: raw x/y/z counts, little-endian."""
        self.responses['beacon_accel_data']({
            'start_clock': clock, 'delta_clock': BEACON_TICKS * (len(counts) - 1),
            'data': b''.join(struct.pack('<hhh', *xyz) for xyz in counts)})


@pytest.mark.parametrize('source', [source for source in fetched('webhooks.py')
                                     if source is None or not source.startswith('kalico')])
def test_beacon_streams_through_the_real_api_server(source, monkeypatch, capsys):
    """Beacon's own endpoint and batch shape (a bare sample list) through the real
    beacon.py and each release's API server: [beacon] is the probe without a sensor
    name, [beacon sensor tool] the one named 'tool'. Beacon registers its
    ACCELEROMETER_* commands through adxl345.py, taken from Klipper master here."""
    require(source)
    helpers = [name for name in fetched('adxl345.py') if name and name.startswith('klipper')]
    require(helpers[0] if helpers else None)
    gcode_module = load_gcode(source)
    webhooks = load_webhooks(source, gcode_module)
    printer, _, _ = ready_dispatch(gcode_module, GCONF_STEALTH)
    printer.objects['webhooks'] = webhooks.WebHooks(printer)
    module = load_beacon(helpers[0], monkeypatch)
    tracker = module.BeaconTracker(None, printer)
    # at rest on the default 16g scale: 1 g is 128 counts of 7.81 mg
    probes = [('beacon', Beacon(module, printer, tracker), (1, -2, 128)),
              ('beacon sensor tool', Beacon(module, printer, tracker, 'tool'), (3, 0, -128))]
    clients = []
    try:
        for chip, probe, _ in probes:
            clients.append(api_client(printer, webhooks))
            clients[-1][0].subscribe_accel(chip)
            assert probe.accel.is_measuring()
        clock = 1000 * BEACON_TICKS
        for _, probe, counts in probes:
            probe.report(clock, [counts] * 8)
        printer.reactor.fire_timers()
        times = [(clock + i * BEACON_TICKS) / BEACON_CLOCK for i in range(8)]
        for (kl, _), (_, _, counts) in zip(clients, probes):
            kl.wait_for_sample(times[-1] - 1e-6, timeout=2.0)
            samples = kl.samples_between(0.0, times[-1] + 1.0)
            assert [sample[0] for sample in samples] == pytest.approx(times)
            assert samples[-1][1:] == pytest.approx([count * 7.81 * 9.80655 for count in counts])
            assert abs(samples[-1][3]) == pytest.approx(9806.65, rel=0.01)      # mm/s^2
            assert kl.overflows == 0
        # a sensor name for the plain [beacon] is refused: that is why none is sent
        with pytest.raises(KlippyError, match="sensor 'beacon' not found"):
            clients[0][0].request('beacon/dump_accel',
                                  {'sensor': 'beacon', 'response_template': {'key': 'other'}})
        assert 'malformed' not in capsys.readouterr().out
    finally:
        for kl, connection in clients:
            kl.close()
            connection.close()


BEACON_CHIPS = [
    # accel_chip, the probe's sensor name and options, its settings as Klipper reports them
    ('beacon', None, {}, {}),
    ('beacon', None, {'accel_name': 'probe'}, {'beacon': {'accel_name': 'probe'}}),
    ('beacon sensor tool', 'tool', {}, {'beacon sensor tool': {'accel_name': 'beacon_tool'}}),
]


@pytest.mark.parametrize('source', fetched('adxl345.py'))
@pytest.mark.parametrize('chip, name, options, settings', BEACON_CHIPS)
def test_beacon_measures_under_the_chip_name_we_send(source, chip, name, options, settings,
                                                     monkeypatch):
    """--csv runs ACCELEROMETER_MEASURE CHIP=...: Beacon registers it through the
    release's own adxl345.py by accel_name, not by the section's last word."""
    require(source)
    gcode_module = load_gcode(source)
    printer, dispatch, _ = ready_dispatch(gcode_module, GCONF_STEALTH)
    printer.objects['webhooks'] = types.SimpleNamespace(register_endpoint=lambda path, callback: None)
    printer.objects['toolhead'] = types.SimpleNamespace(get_last_move_time=lambda: 0.0)
    module = load_beacon(source, monkeypatch)
    Beacon(module, printer, module.BeaconTracker(None, printer), name, **options)
    console = []
    dispatch.register_output_handler(console.append)
    dispatch.run_script('ACCELEROMETER_MEASURE CHIP=%s NAME=x' % accel_command_chip(settings, chip))
    assert console == ['// accelerometer measurements started']
    last_word = chip.split()[-1]
    if last_word != accel_command_chip(settings, chip):
        with pytest.raises(gcode_module.CommandError, match='not valid for CHIP'):
            dispatch.run_script('ACCELEROMETER_MEASURE CHIP=%s NAME=y' % last_word)


RIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'toolhead_rig.py')


def rig(source: str, **scenario) -> dict:
    """The stress strokes through this release's own motion planner (toolhead_rig.py)."""
    run = subprocess.run([sys.executable, '-W', 'ignore', RIG, os.path.join(SRC, source),
                          json.dumps(dict({'max_velocity': 500, 'center': 150}, **scenario))],
                         capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


STROKE_CASES = [
    # kinematics, motor, accel, span, max_velocity, the envelope's speed ladder
    ('cartesian', 'x', 3000, 25.0, 500, (150, 200, 250, 300, 350)),
    ('cartesian', 'x', 1000, 25.0, 500, (150, 200, 250, 300)),
    ('corexy', 'x', 1500, 25.0, 500, (150, 250, 350)),
    ('cartesian', 'y', 600, 15.0, 500, (100, 150, 200)),
    ('cartesian', 'x', 3000, 25.0, 160, (150, 200)),        # max_velocity caps the head
    ('corexy', 'y', 3000, 25.0, 200, (250, 300)),           # a belt runs sqrt2 times the head
]


def stroke_speeds(rung) -> 'list[float]':
    return [stroke['speed'] for stroke in rung['strokes']]


@pytest.mark.parametrize('source', fetched('toolhead.py'))
@pytest.mark.parametrize('kinematics, motor, accel, span, max_velocity, speeds', STROKE_CASES)
def test_every_rung_the_envelope_keeps_is_reached(source, kinematics, motor, accel, span,
                                                   max_velocity, speeds):
    # a rung the strokes never reached 'held' and topped the max_velocity advice
    require(source)
    from chopper_autotune.current import belt_cap, belt_top, velocity_caps
    from chopper_autotune.envelope import stroke_ladders
    _, vec, kept, _, _ = stroke_ladders(kinematics, motor, span * 8, speeds, accel, (accel,), 0,
                                        max_velocity)
    result = rig(source, tool='envelope', kinematics=kinematics, motor=motor, max_accel=accel,
                 max_velocity=max_velocity, span=span, rungs=[[speed, accel] for speed in speeds])
    assert result['freed']['minimum_cruise_ratio'] == 0
    assert result['restored'] == result['limits']
    for rung in result['rungs']:
        strokes = rung['strokes']
        assert len(strokes) == 2 * STRESS_REPS
        # every stroke full-length, at the rung's accel: a half one from the center peaks lower
        assert [stroke['length'] for stroke in strokes] == pytest.approx(
            [2 * span * math.hypot(*vec)] * len(strokes))
        assert [stroke['accel'] for stroke in strokes] == pytest.approx([accel] * len(strokes))
        expected = rung['speed'] if rung['speed'] in kept else belt_top(
            span, vec, accel, belt_cap(velocity_caps(kinematics, vec, max_velocity)))
        assert stroke_speeds(rung) == pytest.approx([expected] * len(strokes), abs=0.5), rung


@pytest.mark.parametrize('source', fetched('toolhead.py'))
def test_every_accel_rung_the_envelope_keeps_reaches_the_probe_speed(source):
    require(source)
    from chopper_autotune.envelope import stroke_ladders
    accels, probe = (300, 500, 1000, 2000), 150
    _, _, _, kept, _ = stroke_ladders('cartesian', 'x', 400.0, (150,), 3000, accels, probe, 500)
    assert kept == (500, 1000, 2000)
    result = rig(source, tool='envelope', kinematics='cartesian', motor='x', max_accel=3000,
                 span=25.0, rungs=[[probe, accel] for accel in accels])
    for rung in result['rungs']:
        assert [stroke['accel'] for stroke in rung['strokes']] == pytest.approx(
            [rung['accel']] * len(rung['strokes']))
        if rung['accel'] in kept:
            assert stroke_speeds(rung) == pytest.approx([probe] * len(rung['strokes']), abs=0.5)
        else:
            assert max(stroke_speeds(rung)) < probe - 0.5


@pytest.mark.parametrize('source', fetched('toolhead.py'))
def test_klipper_brakes_the_strokes_early_unless_freed(source):
    # the rig sees the reported ceiling: minimum_cruise_ratio 0.5 stops a 50 mm stroke at
    # 3000 mm/s2 at sqrt(50 * 1500) = 274 mm/s
    require(source)
    result = rig(source, tool='envelope', kinematics='cartesian', motor='x', max_accel=3000,
                 span=25.0, rungs=[[300, 3000]], free=False)
    assert stroke_speeds(result['rungs'][0]) == pytest.approx([273.9] * 2 * STRESS_REPS, abs=0.1)


@pytest.mark.parametrize('source', fetched('toolhead.py'))
def test_a_speed_factor_left_from_a_print_is_undone_for_the_strokes(source):
    require(source)
    freed = rig(source, tool='envelope', kinematics='cartesian', motor='x', max_accel=3000,
                span=25.0, rungs=[[200, 3000]], before=['M220 S80'])
    assert (freed['limits']['speed_factor'], freed['freed']['speed_factor'],
            freed['restored']['speed_factor']) == pytest.approx((0.8, 1.0, 0.8))
    assert stroke_speeds(freed['rungs'][0]) == pytest.approx([200] * 2 * STRESS_REPS, abs=0.5)
    left = rig(source, tool='envelope', kinematics='cartesian', motor='x', max_accel=3000,
               span=25.0, rungs=[[200, 3000]], before=['M220 S80'], free=False)
    assert stroke_speeds(left['rungs'][0]) == pytest.approx([160] * 2 * STRESS_REPS, abs=0.5)


@pytest.mark.parametrize('source', fetched('toolhead.py'))
@pytest.mark.parametrize('kinematics, accel', [('cartesian', 1000), ('corexy', 500)])
def test_every_belt_speed_of_the_current_pattern_is_reached(source, kinematics, accel):
    require(source)
    result = rig(source, tool='current', kinematics=kinematics, motor='x', max_accel=accel,
                 accel=accel, span=25.0)
    assert [rung['speed'] for rung in result['rungs']] == list(BELT_SPEEDS)
    length = 2 * 25.0 * math.hypot(*stress_vector(kinematics, 'x'))
    for rung in result['rungs']:
        strokes = rung['strokes']
        assert stroke_speeds(rung) == pytest.approx([rung['speed']] * len(strokes), abs=0.5)
        assert [stroke['length'] for stroke in strokes] == pytest.approx([length] * len(strokes))
        assert [stroke['accel'] for stroke in strokes] == pytest.approx([accel] * len(strokes))


G1 = {'tool': 'g1', 'motor': 'x', 'max_velocity': 1500, 'max_accel': 500, 'accel': 500,
      'measure_time': 1.0, 'trim': 0.1}
G1_SPEEDS = [20, 40, 60, 80, 100, 120]


@pytest.mark.parametrize('source', fetched('toolhead.py'))
@pytest.mark.parametrize('kinematics, motor', [('cartesian', 'x'), ('corexy', 'x'), ('corexy', 'y')])
def test_a_g1_stroke_runs_the_force_move_trapezoid_and_the_window_lies_in_its_cruise(
        source, kinematics, motor):
    # the motors of a rail move together only on G1 (#129): the window the tools cut back
    # from the stroke's end must land in its cruise as on a FORCE_MOVE; on CoreXY's
    # diagonal the other belt stands
    require(source)
    result = rig(source, **dict(G1, kinematics=kinematics, motor=motor, speeds=G1_SPEEDS))
    for speed, stroke in zip(G1_SPEEDS, result['rungs']):
        accel_t, cruise_t, top = stroke['force_move']
        assert top == speed
        assert (stroke['speed'], stroke['accel'], stroke['other'], stroke['start_v']) \
            == pytest.approx((speed, 500, 0, 0), abs=1e-3)
        assert (stroke['accel_t'], stroke['cruise_t'], stroke['decel_t']) \
            == pytest.approx((accel_t, cruise_t, accel_t), abs=1e-6)
        assert stroke['end'] == pytest.approx(0, abs=1e-9)        # M400's print time: its end
        assert min(stroke['window']) > 0


G1_DEVIATIONS = {
    # what is left in force, and the belt speed the planner gives the stroke instead
    'minimum_cruise_ratio 0.5': (dict(accel=50, speeds=[60, 120]),
                                 lambda speed: math.sqrt(collect.travel_for(speed, 50, 1.0) * 50 * .5)),
    'M220 S80': (dict(before=['M220 S80'], speeds=[60]), lambda speed: .8 * speed),
    'max_velocity': (dict(kinematics='corexy', max_velocity=80, speeds=[120]),
                     lambda speed: belt_cap(velocity_caps('corexy', stress_vector('corexy', 'x'), 80))),
}


@pytest.mark.parametrize('source', fetched('toolhead.py'))
@pytest.mark.parametrize('limit', G1_DEVIATIONS)
def test_klipper_runs_a_g1_stroke_slower_with_the_window_still_in_its_cruise(source, limit):
    # nothing in the stroke tells: what holds it back must be lifted or refused before
    # the first move, or the dataset files the planned speed under a slower one
    require(source)
    scenario, runs = G1_DEVIATIONS[limit]
    result = rig(source, **{**G1, 'kinematics': 'cartesian', 'free': False, **scenario})
    for speed, stroke in zip(scenario['speeds'], result['rungs']):
        assert stroke['speed'] == pytest.approx(runs(speed), abs=0.5)
        assert stroke['speed'] < speed - 1
        assert min(stroke['window']) > 0


@pytest.mark.parametrize('source', fetched('toolhead.py'))
@pytest.mark.parametrize('limit', ['minimum_cruise_ratio 0.5', 'M220 S80'])
def test_freeing_the_strokes_lets_a_g1_stroke_reach_its_speed(source, limit):
    require(source)
    scenario, _ = G1_DEVIATIONS[limit]
    result = rig(source, **{**G1, 'kinematics': 'cartesian', **scenario})
    assert [stroke['speed'] for stroke in result['rungs']] == pytest.approx(scenario['speeds'], abs=0.5)
    assert all(min(stroke['window']) > 0 for stroke in result['rungs'])


@pytest.mark.parametrize('source', fetched('input_shaper.py'))
def test_the_input_shaper_frequencies_come_back_as_klipper_reports_them(source):
    """SET_INPUT_SHAPER without parameters reports each axis; free_strokes reads the
    frequencies from that report to put them back after the strokes."""
    require(source)
    with open(os.path.join(SRC, source, 'input_shaper.py')) as module:
        tree = ast.parse(module.read())
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name in ('InputShaperParams', 'AxisInputShaper')]
    namespace = {'collections': collections}
    exec(compile(ast.Module(body=classes, type_ignores=[]), 'input_shaper.py', 'exec'), namespace)
    printed = []
    for axis, freq in (('x', 40.0), ('y', 0.0)):
        params = namespace['InputShaperParams'].__new__(namespace['InputShaperParams'])
        params.axis, params.shaper_type, params.shaper_freq, params.damping_ratio = axis, 'mzv', freq, 0.1
        shaper = namespace['AxisInputShaper'].__new__(namespace['AxisInputShaper'])
        shaper.axis, shaper.params = axis, params
        shaper.report(types.SimpleNamespace(respond_info=printed.append))
    kl = types.SimpleNamespace(gcode_output=lambda script: ['// ' + line for line in printed])
    assert shaper_freqs(kl) == {'x': '40.000', 'y': '0.000'}


class Popen:
    """What RUN_SHELL_COMMAND starts: the argv is recorded, the process ends at once."""
    started = []

    def __init__(self, argv, stdout=None, stderr=None):
        Popen.started.append(list(argv))
        read, write = os.pipe()
        os.close(write)
        self.stdout = os.fdopen(read, 'rb')

    def poll(self):
        return 0

    def terminate(self):
        pass


class ShellReactor(Reactor):
    def pause(self, waketime):
        return waketime


def macro_printer(source: str, *configs: str, ours: str = CFG):
    """Our chopper_autotune.cfg, and `configs` read after it, on this release's own
    gcode.py, config reader, gcode_macro.py, delayed_gcode.py and display_status.py,
    with the shell command module the printer runs: Kalico ships one, install.sh puts
    ours beside Klipper's extras (and leaves one already there)."""
    gcode = load_gcode(source)
    configfile = load_klippy(source, 'configfile')
    if source.startswith('kalico'):
        sys.modules['klippy'] = types.SimpleNamespace(configfile=configfile)   # 'from klippy import'
    macro = load_klippy(source, 'gcode_macro')
    shell_path = (os.path.join(SRC, source, 'gcode_shell_command.py') if source.startswith('kalico')
                  else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    'gcode_shell_command.py'))
    spec = importlib.util.spec_from_file_location('contract_shell_%s' % source.replace('-', '_')
                                                  .replace('.', '_'), shell_path)
    shell = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(shell)
    shell.subprocess = types.SimpleNamespace(Popen=Popen, PIPE=-1, STDOUT=-2)
    fileconfig = read_like_klipper(ours, *configs)
    printer = Printer()
    printer.reactor = ShellReactor()
    printer.command_error = gcode.CommandError
    printer.config_error = configparser.Error
    dispatch = gcode.GCodeDispatch(printer)
    printer.objects['gcode'] = dispatch
    printer.objects['configfile'] = types.SimpleNamespace(
        get_status=lambda eventtime: {'settings': settings_of(fileconfig)})
    printer.objects['display_status'] = load_klippy(source, 'display_status').load_config(
        ConsoleConfig(printer))
    tracking = {}
    modules = {'gcode_macro': macro, 'gcode_shell_command': shell,
               'delayed_gcode': load_klippy(source, 'delayed_gcode')}

    def load_object(config, section, default=None):
        if section not in printer.objects:
            wrapper = configfile.ConfigWrapper(printer, fileconfig, tracking, section)
            module = modules[section.split()[0]]
            printer.objects[section] = (module.load_config_prefix(wrapper) if ' ' in section
                                        else module.load_config(wrapper))
        return printer.objects[section]
    printer.load_object = load_object
    printer.lookup_objects = lambda module=None: [
        (name, obj) for name, obj in printer.objects.items()
        if module is None or name.split()[0] == module]
    for section in fileconfig.sections():
        if section.split()[0] in modules:
            load_object(None, section)
    printer.send_event('klippy:ready')
    console = []
    dispatch.register_output_handler(console.append)
    return dispatch, console


MACRO_LINES = [
    # what the user types, and what the tool gets
    ('CHOPPER_COLLECT SPEED=55 DRY_RUN=1 ; my note', ['SPEED=55', 'DRY_RUN=1']),
    ('chopper_collect speed=55 dry_run=1', ['SPEED=55', 'DRY_RUN=1']),
    ("CHOPPER_ANALYZE DATASET='/home/pi/my set'", ['DATASET=/home/pi/my set']),
    ('CHOPPER_ANALYZE DATASET="/home/pi/my set" TOP=5', ['DATASET=/home/pi/my set', 'TOP=5']),
    ('CHOPPER_ANALYZE HTML="/home/pi/it\'s.html"', ["HTML=/home/pi/it's.html"]),
    ('CHOPPER_ANALYZE HTML=\'say "hi".html\'', ['HTML=say "hi".html']),
    ('CHOPPER_BELTS MU=', ['MU=']),
    # a backslash before a quote, and a ';' inside the quotes
    (r"""CHOPPER_ANALYZE HTML='a\\"b;c\\\\d'""", [r'HTML=a\\"b;c\\\\d']),
    ('CHOPPER_STATUS', []),
]


@pytest.mark.parametrize('source', fetched('gcode_macro.py'))
@pytest.mark.parametrize('line, argv', MACRO_LINES)
def test_a_macro_line_reaches_the_tool_as_typed(source, line, argv):
    # {rawparams} carried a '; comment' into the tool's arguments, and a value in single
    # quotes broke the macro's own quoting
    require(source)
    dispatch, console = macro_printer(source)
    Popen.started = []
    dispatch.run_script(line)
    assert [started[1:] for started in Popen.started] == [argv], (source, console)
    assert not [text for text in console if text.startswith('!!')], console


@pytest.mark.parametrize('source', fetched('delayed_gcode.py'))
def test_the_self_check_names_a_replaced_macro_once(source, tmp_path):
    # Klipper prints an error once at every macro level it passes: from a macro the
    # delayed_gcode called, the console got the self-check's error twice
    require(source)
    other = tmp_path / 'other.cfg'
    other.write_text('[gcode_macro CHOPPER_TUNE]\ngcode:\n    G28\n')
    dispatch, console = macro_printer(source, str(other))
    dispatch.printer.reactor.fire_timers()                  # initial_duration has passed
    errors = [message for message in console if message.startswith('!!')]
    assert len(errors) == 1 and 'chopper-autotune: CHOPPER_TUNE runs another tool' in errors[0]
    message = dispatch.printer.objects['display_status'].get_status(0)['message']
    assert message == 'Clash TUNE'


@pytest.mark.parametrize('source', fetched('delayed_gcode.py'))
def test_the_self_check_survives_a_section_of_ours_removed_by_hand(source, tmp_path):
    # a template error in a delayed_gcode reaches only klippy.log: a missing section
    # of ours must neither break the check nor count as a conflict
    require(source)
    ours = tmp_path / 'chopper_autotune.cfg'
    with open(CFG) as original:
        ours.write_text(original.read().replace('[gcode_macro CHOPPER_EXTRUDER]',
                                                '[gcode_macro TUNE_EXTRUDER]'))
    dispatch, console = macro_printer(source, ours=str(ours))
    dispatch.printer.reactor.fire_timers()
    assert console == []
    other = tmp_path / 'other.cfg'
    other.write_text('[gcode_macro CHOPPER_TUNE]\ngcode:\n    G28\n')
    dispatch, console = macro_printer(source, str(other), ours=str(ours))
    dispatch.printer.reactor.fire_timers()
    errors = [message for message in console if message.startswith('!!')]
    assert len(errors) == 1 and 'chopper-autotune: CHOPPER_TUNE runs another tool' in errors[0]


@pytest.mark.parametrize('source', fetched('delayed_gcode.py'))
def test_the_self_check_is_silent_without_a_conflict(source):
    require(source)
    dispatch, console = macro_printer(source)
    dispatch.printer.reactor.fire_timers()
    assert console == [] and not dispatch.printer.objects['display_status'].get_status(0)['message']


class ConsoleConfig:
    """[respond] / [display_status] with nothing set: every option at its default."""

    def __init__(self, printer):
        self.printer = printer

    def get_printer(self):
        return self.printer

    def getchoice(self, option, choices, default=None):
        return choices[default]

    def get(self, option, default=None):
        return default

    def getboolean(self, option, default=None):
        return default


SHUTDOWN_MESSAGE = ("TMC 'stepper_x' reports error: GSTAT: 00000002 drv_err=1(ErrorShutdown!)\n"
                    'Once the underlying issue is corrected, use the "FIRMWARE_RESTART"\n'
                    'command to reset the firmware, reload the config, and restart the host software.\n'
                    'Printer is shutdown')


def console_printer(source: str, respond: bool, display: bool, shutdown: bool = False):
    """This release's own gcode.py with or without its respond.py and display_status.py,
    ready or shut down; our client's view of it (gcode/script, objects/query of gcode)."""
    gcode_module = load_gcode(source)
    printer = Printer()
    printer.command_error = gcode_module.CommandError
    dispatch = gcode_module.GCodeDispatch(printer)
    printer.objects['gcode'] = dispatch
    console = []
    dispatch.register_output_handler(console.append)
    status = None
    if respond:
        load_klippy(source, 'respond').load_config(ConsoleConfig(printer))
    if display:
        status = load_klippy(source, 'display_status').load_config(ConsoleConfig(printer))
    printer.send_event('klippy:ready')
    if shutdown:
        printer.get_state_message = lambda: (SHUTDOWN_MESSAGE, 'shutdown')
        printer.send_event('klippy:shutdown')
    del console[:]

    def gcode(script):
        try:
            dispatch.run_script(script)
        except gcode_module.CommandError as why:     # webhooks answers with an error
            raise KlippyError('gcode/script failed: %s' % why)
    kl = types.SimpleNamespace(gcode=gcode, connect=lambda: kl, close=lambda: None,
                               request=lambda method, params: {'status': {'gcode': {
                                   'commands': dispatch.get_status(0.)['commands']}}})
    return kl, console, status


@pytest.mark.parametrize('source', fetched('respond.py'))
@pytest.mark.parametrize('respond, display', [(True, True), (True, False), (False, True),
                                              (False, False)])
def test_the_screen_writes_only_where_klipper_takes_it(source, respond, display):
    # an unknown command is no error in Klipper: without [respond] every update printed
    # 'Unknown command', and a quote in a text ended RESPOND's MSG
    require(source)
    kl, console, status = console_printer(source, respond, display)
    screen = collect.Screen(kl, True)
    screen.update('Chopper 42% 17/40 ETA 1:05', force=True, short='A 42% ETA 1:05')
    screen.update('WARNING: a "quoted" note (issue #129)', force=True, short='AWD: approx.')
    screen.final('Tune done: A 2/3/5/0 -42%', 'Done A-42%')
    assert not [line for line in console if 'Unknown command' in line or line.startswith('!!')]
    # one space after the prefix, no 'Chopper: Chopper', one console line for the verdict;
    # the console keeps the whole text, the display what a 16-character LCD row shows
    assert console == (["Chopper: 42% 17/40 ETA 1:05", "Chopper: WARNING: a 'quoted' note (issue #129)",
                        'echo: Tune done: A 2/3/5/0 -42%'] if respond else [])
    if display:
        assert status.message == 'Done A-42%'


@pytest.mark.parametrize('source', fetched('respond.py'))
def test_a_failure_after_a_shutdown_reaches_the_console_alone(source, monkeypatch):
    # Klipper in shutdown takes M118, not M117: a refused M117 reprinted the whole state
    require(source)
    import chopper_autotune.klippy as klippy_mod
    from chopper_autotune.cli import announce_failure
    kl, console, _ = console_printer(source, True, True, shutdown=True)
    monkeypatch.delenv('CHOPPER_SYNC', raising=False)
    monkeypatch.setattr(klippy_mod, 'Klippy', lambda path: kl)
    monkeypatch.setattr(klippy_mod, 'find_socket', lambda explicit=None: '<sock>')
    announce_failure(types.SimpleNamespace(socket=None), 'tune FAILED: Klipper shut down')
    assert console == ['echo: tune FAILED: Klipper shut down']



def limited_kinematics(source: str, name: str, **state):
    """Kalico's limited_corexy or limited_cartesian, with the state its __init__ reads
    from [printer]; the base class it extends only checks endstops here."""
    tag = 'contract_%s_kin' % source.replace('.', '_').replace('-', '_')
    package = sys.modules.get(tag) or types.ModuleType(tag)
    package.__path__ = [os.path.join(SRC, source)]
    sys.modules[tag] = package
    base_name, class_name = (('corexy', 'CoreXYKinematics') if name == 'limited_corexy'
                             else ('cartesian', 'CartKinematics'))
    base = types.ModuleType('%s.%s' % (tag, base_name))
    setattr(base, class_name, type(class_name, (), {'_check_endstops': lambda self, move: None}))
    setattr(package, base_name, base)
    sys.modules[base.__name__] = base
    spec = importlib.util.spec_from_file_location('%s.%s' % (tag, name),
                                                  os.path.join(SRC, source, name + '.py'))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    cls = module.LimitedCoreXYKinematics if name == 'limited_corexy' else module.LimitedCartKinematics
    kinematics = cls.__new__(cls)
    kinematics.__dict__.update(state)
    return kinematics


class KinematicsKl:
    """Our client's view of a dispatcher: scripts run, gcode_output returns the console."""

    def __init__(self, dispatch):
        self.dispatch, self.console = dispatch, []
        dispatch.register_output_handler(self.console.append)

    def gcode(self, script):
        self.dispatch.run_script(script)

    def gcode_output(self, script):
        del self.console[:]
        self.dispatch.run_script(script)
        return list(self.console)


class StrokeMove:
    """One stress stroke as Kalico's check_move sees it: the limits it sets are kept."""

    def __init__(self, vec, max_velocity, accel, length=50.0):
        unit = math.hypot(*vec)
        self.move_d = length
        self.axes_d = (length * vec[0] / unit, length * vec[1] / unit, 0.0)
        self.axes_r = (vec[0] / unit, vec[1] / unit, 0.0)
        self.is_kinematic_move = True
        self.toolhead = types.SimpleNamespace(get_max_velocity=lambda: (max_velocity, accel))
        self.limits = None

    def limit_speed(self, speed, accel):
        self.limits = (speed, accel)


LIMITED = [
    ('limited_corexy', {'max_x_accel': 3000.0, 'max_y_accel': 2000.0, 'max_z_accel': 100.0,
                        'max_z_velocity': 15.0, 'scale_per_axis': True}),
    ('limited_cartesian', {'max_velocities': [300.0, 200.0, 15.0],
                           'max_accels': [3000.0, 2000.0, 100.0], 'xy_hypot_accel': math.hypot(3000, 2000),
                           'scale_per_axis': True, 'config_max_velocity': 400.0,
                           'config_max_accel': 5000.0}),
]


@pytest.mark.parametrize('name, state', LIMITED)
@pytest.mark.parametrize('motor', ['x', 'y'])
def test_the_strokes_get_what_the_tools_plan_under_kalicos_per_axis_limits(name, state, motor):
    # Kalico caps each axis whatever M204 asks: CURRENT loads a motor as the printer does,
    # the envelope lifts the accel limits its rungs run past, and both put them back
    from chopper_autotune.current import (accel_along, axis_limits, belt_cap, keep_axis_limits,
                                          lift_axis_limits, velocity_caps)
    source = next((s for s in fetched('limited_corexy.py') if s and s.startswith('kalico')), None)
    require(source)
    kinematics = limited_kinematics(source, name, **copy.deepcopy(state))
    _, dispatch, _ = ready_dispatch(load_gcode(source), GCONF_STEALTH)
    dispatch.register_command('SET_KINEMATICS_LIMIT', kinematics.cmd_SET_KINEMATICS_LIMIT)
    kl = KinematicsKl(dispatch)
    vec, max_velocity, accel = stress_vector(name, motor), 400.0, 12000.0
    limits = axis_limits(kl, name)

    def stroke(along=vec):
        # (the belt speed, the accel) a move along this direction gets at M204 S<accel>
        move = StrokeMove(along, max_velocity, accel)
        kinematics.check_move(move)
        speed, cap = move.limits
        return min(speed, max_velocity) * math.hypot(*along), min(cap, accel)

    belt, stroke_accel = stroke()
    assert belt == pytest.approx(belt_cap(velocity_caps(name, vec, max_velocity, limits)))
    assert stroke_accel == pytest.approx(accel_along(name, vec, accel, limits))
    assert stroke_accel < accel                     # the per-axis limit would cut a rung

    restores = []
    keep_axis_limits(kl, limits, restores)
    lift_axis_limits(kl, name, limits, motor)
    assert stroke() == pytest.approx((belt, accel))  # the rung's accel; the velocity limits stay
    if name == 'limited_cartesian':
        # the other axis keeps its limit for the moves that set the strokes up
        other = (0.0, 1.0) if motor == 'x' else (1.0, 0.0)
        assert stroke(other)[1] == pytest.approx(accel_along(name, other, accel,
                                                             dict(limits, scale=False)))
    for restore in restores:
        restore()
    assert {key: getattr(kinematics, key) for key in state} == state
