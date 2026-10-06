"""A printer of a fetched Klipper or Kalico release, built from the release's own modules,
for the tools to run against: the G-code dispatcher, respond and display_status (the
console and the display), stepper_enable, force_move, gcode_move, the toolhead with its
lookahead, and every TMC section as its own driver class (the real FieldHelper, current
helper and commands). A tool talks to it through our own Klippy client over a socket
pair, the requests the API server takes; a save, through FrontMoonraker.

Stand-ins, where Klipper meets the hardware: the MCU and the step generation (a stepper
only records that it stepped; a toolhead move is recorded as the release queues it), the
TMC chip (it keeps what is written and answers a read with it), the kinematics (the homing
state, the range check, where the head is, the rails a move turns), G28 (each axis lands
on its endstop), a FORCE_MOVE's motion (recorded; the toolhead dwells as long as it would
run), a heater reaching its target in a second, and an accelerometer streaming the
vibration of a modeled printer (shake()). Options no real module here reads land in the
settings the way Klipper records them, numbers as floats.
"""
import ast
import configparser
import contextlib
import importlib
import importlib.util
import json
import math
import os
import socket
import sys
import threading
import time
import types
from unittest import mock

from chopper_autotune import tmc
from chopper_autotune.klippy import Klippy
from chopper_autotune.moonraker import MoonrakerError

SRC = os.environ.get('KLIPPER_SRC_DIR') or os.path.join(os.path.dirname(__file__), '.klipper-src')
SAMPLE_HZ = 400.0
STANDSTILL_MG = 5.0
MOVING = 1e-6               # mm, mm/s: a belt that runs less stands
TMC_SECTIONS = ('tmc2130', 'tmc2208', 'tmc2209', 'tmc2240', 'tmc2660', 'tmc5160')
CHOPPER_FIELDS = ('tbl', 'toff', 'hstrt', 'hend', 'tpfd', 'en_spreadcycle', 'en_pwm_mode', 'chm')
STEPPER_SECTIONS = ('stepper_', 'extruder', 'manual_stepper ', 'dual_carriage')
# what a release's modules import by name: klippy/*.py, its packages, Kalico's klippy
KLIPPY_NAMES = {'klippy', 'gcode', 'toolhead', 'mcu', 'chelper', 'stepper', 'extras', 'kinematics'}
# the extras built from the release's own module; any other one an object loads (homing,
# statistics, verify_heater...) is a bare stand-in the tools never meet
REAL_EXTRAS = {'gcode_move', 'display_status', 'stepper_enable', 'force_move', 'respond', 'heaters'}
CLOCK = [0.]                # the seconds the tools slept (sleep()): Klipper's time ran on


class ConfigError(Exception):
    pass


def boolean(text: str) -> bool:
    return text.strip().lower() in ('1', 'yes', 'true', 'on')


def option_value(text: str):
    """An option no real module reads here, as Klipper records a number: a float."""
    try:
        return float(text)
    except ValueError:
        return text


class Config:
    """A section as Klipper's config reader serves it: every option read lands in the
    settings with its value, a default too (configfile's access tracking)."""
    error = ConfigError
    required = object()

    def __init__(self, front, section: str):
        self.front, self.section = front, section

    def get_printer(self):
        return self.front.printer

    def get_name(self):
        return self.section

    def has_section(self, section):
        return self.front.fileconfig.has_section(section)

    def getsection(self, section):
        return Config(self.front, section)

    def get_prefix_sections(self, prefix):
        return [Config(self.front, section) for section in self.front.fileconfig.sections()
                if section.startswith(prefix)]

    def _get(self, parse, option, default):
        key = (self.section.lower(), option.lower())
        if not self.front.fileconfig.has_option(self.section, option):
            if default is self.required:
                raise ConfigError("Option '%s' in section '%s' must be specified"
                                  % (option, self.section))
            if default is not None:
                self.front.tracking[key] = default
            return default
        value = parse(self.front.fileconfig.get(self.section, option))
        self.front.tracking[key] = value
        return value

    def get(self, option, default=required, note_valid=True):
        return self._get(str, option, default)

    def getint(self, option, default=required, minval=None, maxval=None, note_valid=True):
        return self._get(int, option, default)

    def getfloat(self, option, default=required, minval=None, maxval=None, above=None,
                 below=None, note_valid=True):
        return self._get(float, option, default)

    def getboolean(self, option, default=required, note_valid=True):
        return self._get(boolean, option, default)

    def getchoice(self, option, choices, default=required, note_valid=True):
        if isinstance(next(iter(choices)), int):
            choice = self.getint(option, default)
        else:
            choice = self.get(option, default)
        if choice not in choices:
            raise ConfigError("Choice '%s' for option '%s' in section '%s' is not a valid choice"
                              % (choice, option, self.section))
        return choices[choice]


def sleep(seconds: float):
    """time.sleep for the tools: the printer's time runs on, nothing waits."""
    CLOCK[0] += seconds


class Clock:
    """A tool module's `time`, its sleep() the printer's (sleep())."""
    sleep = staticmethod(sleep)

    def __getattr__(self, name):
        return getattr(time, name)


class Reactor:
    """Klipper's reactor on the printer's clock: the toolhead's print time plus what the
    tools slept. A callback runs once the script that queued it has returned or a move
    steps the motor (TMC's enable handling takes the G-code mutex); of the timers a TMC
    driver's status check runs, when it is due (once a second for an enabled driver)."""
    NOW = 0.
    NEVER = 9e15

    def __init__(self, front):
        self.front = front
        self.callbacks, self.timers = [], {}
        self.pauses = 0

    def monotonic(self):
        toolhead = getattr(self.front, 'toolhead', None)
        return CLOCK[0] + (toolhead.print_time if toolhead is not None else 0.)

    def pause(self, waketime):
        """A wait (TEMPERATURE_WAIT): the time runs on, the hotend reaches its target."""
        self.pauses += 1
        if self.pauses > 1000:
            raise RuntimeError('a wait that never ends')
        CLOCK[0] += max(0., waketime - self.monotonic())
        self.front.heat(waketime)
        return waketime

    def register_timer(self, callback, waketime=NEVER):
        self.timers[callback] = waketime
        return callback

    def update_timer(self, timer, waketime):
        self.timers[timer] = waketime

    def unregister_timer(self, timer):
        self.timers.pop(timer, None)

    def poll_drivers(self):
        now = self.monotonic()
        for timer, waketime in list(self.timers.items()):
            if waketime <= now and type(getattr(timer, '__self__', None)).__name__ == 'TMCErrorCheck':
                self.timers[timer] = timer(now)

    def register_callback(self, callback, waketime=NOW):
        self.callbacks.append(callback)

    def run_callbacks(self):
        while self.callbacks:
            self.callbacks.pop(0)(self.monotonic())

    def mutex(self):
        return threading.RLock()

    def assert_no_pause(self):
        return contextlib.nullcontext()


class Mcu:
    """The MCU, writing to a file: the toolhead never waits for it."""
    non_critical_disconnected = False                  # Kalico

    def is_fileoutput(self):
        return True

    def estimated_print_time(self, eventtime):
        return 0.

    def __getattr__(self, name):
        return mock.MagicMock(name='mcu.' + name)


class MotionQueuing:
    """Klipper master's step-generation queue (toolhead.py and force_move.py ask it)."""

    def allocate_trapq(self):
        return mock.MagicMock(name='trapq')

    def lookup_trapq_append(self):
        return lambda *args: None

    def calc_step_gen_restart(self, est_print_time):
        return 0.

    def __getattr__(self, name):
        return mock.MagicMock(name='motion_queuing.' + name)


class Pins:
    """printer.lookup_object('pins'): a stepper's enable line (a pin named twice is one
    shared line), a heater's output, a TMC's virtual endstop chip."""

    def __init__(self, mcu):
        self.mcu = mcu
        self.params = {}

    def lookup_pin(self, pin, can_invert=False, can_pullup=False, share_type=None):
        name = pin.strip().lstrip('!^~').strip()
        if name not in self.params:
            line = types.SimpleNamespace(setup_max_duration=lambda duration: None,
                                         set_digital=lambda print_time, value: None)
            self.params[name] = {'chip': types.SimpleNamespace(setup_pin=lambda kind, params: line),
                                 'pin': name, 'invert': pin.startswith('!'), 'pullup': 0,
                                 'share_type': share_type}
        return self.params[name]

    def register_chip(self, name, chip):
        pass

    def setup_pin(self, pin_type, pin):
        """A heater's PWM output."""
        return types.SimpleNamespace(setup_cycle_time=lambda *args, **kwargs: None,
                                     setup_max_duration=lambda duration: None,
                                     set_pwm=lambda print_time, value, *args: None,
                                     get_mcu=lambda: self.mcu)


class Thermistor:
    """A heater's temperature sensor: Front.heat() reports to it."""

    def __init__(self, config):
        self.callback = None
        config.front.sensors.append(self)

    def setup_minmax(self, min_temp, max_temp):
        pass

    def setup_callback(self, callback):
        self.callback = callback

    def get_report_time_delta(self):
        return .300


class Stepper:
    """An MCU stepper: its name, and the callbacks Klipper runs at its next step (the
    enable line of stepper_enable)."""

    def __init__(self, name: str):
        self.name = name
        self.active_callbacks = []

    def get_name(self, short=False):
        if short and self.name.startswith('stepper_'):
            return self.name[8:]
        return self.name

    def add_active_callback(self, callback):
        self.active_callbacks.append(callback)

    def step(self, print_time: float):
        callbacks, self.active_callbacks = self.active_callbacks, []
        for callback in callbacks:
            callback(print_time)

    def get_dir_inverted(self):
        return False, 0

    def get_mcu_position(self, *args):
        return 0

    def mcu_to_commanded_position(self, position):
        return 0.

    def setup_default_pulse_duration(self, *args):
        pass

    def get_pulse_duration(self):
        return .000000100, False

    def set_tmc_current_helper(self, helper):         # Kalico
        self.tmc_current_helper = helper


class Chip:
    """The TMC chip behind its UART or SPI: it keeps each register as written and answers
    a read with it; a status register reads as a quiet driver at work, plus the flags a
    test sets in `reads`."""

    def __init__(self, config, name_to_reg, fields, *rest):
        self.front = config.front
        self.section = config.get_name()
        self.name_to_reg, self.fields = name_to_reg, fields
        self.tmc_frequency = rest[-1] if rest else None
        self.registers, self.reads, self.writes = {}, {}, []
        if 'ADC_TEMP' in name_to_reg:               # a TMC2240's die at the room's 25 C
            self.reads['ADC_TEMP'] = 2038 + int(25 * 7.7)
        self.mcu = self.front.printer.objects['mcu']
        self.front.chips[self.section] = self

    def get_fields(self):
        return self.fields

    def get_register(self, reg_name):
        if reg_name in self.registers:
            return self.registers[reg_name]
        return self.reads.get(reg_name, 0) | self.running(reg_name)

    def running(self, reg_name: str) -> int:
        """A status register of a driver at work: its actual current scale is the run
        current's (0 reads as a driver reset on a TMC2130 or TMC2660)."""
        for field, current in (('cs_actual', 'irun'), ('se', 'cs')):
            mask = self.fields.all_fields.get(reg_name, {}).get(field)
            if mask and self.fields.lookup_register(current) is not None:
                shift = (mask & -mask).bit_length() - 1
                return (max(1, self.fields.get_field(current)) << shift) & mask
        return 0

    def set_register(self, reg_name, val, print_time=None):
        self.registers[reg_name] = val
        self.writes.append((reg_name, val))

    def get_tmc_frequency(self):
        return self.tmc_frequency

    def get_mcu(self):
        return self.mcu

    def field(self, name: str):
        """The value of a field in the register as the chip holds it."""
        register = self.fields.lookup_register(name)
        return self.fields.get_field(name, self.registers.get(register, 0), register)

    def chopper(self) -> dict:
        """The chopper fields and the mode bit the chip holds now."""
        return {name: self.field(name) for name in CHOPPER_FIELDS
                if self.fields.lookup_register(name) is not None}


class BulkHelper:
    """bulk_sensor.BatchBulkHelper: the stallguard dump endpoint, never asked here."""

    def __init__(self, *args, **kwargs):
        pass

    def add_mux_endpoint(self, *args, **kwargs):
        pass


class DangerOptions:
    """Kalico's danger_options at their defaults: times zero, switches off."""

    def __getattr__(self, name):
        return 0. if name.endswith('time') else False


def parse_step_distance(config, units_in_radians=None, note_valid=False):
    """stepper.parse_step_distance: (rotation_distance, steps per rotation)."""
    microsteps = config.getint('microsteps')
    full_steps = config.getint('full_steps_per_rotation', 200)
    return config.getfloat('rotation_distance'), full_steps * microsteps


class Kinematics:
    """What a tool meets of the kinematics: the homing state, the range check of
    cartesian.py and corexy.py (an unhomed axis must not move), and the rails a move
    turns (Front.belts())."""

    def __init__(self, front, toolhead, config):
        self.front = front
        self.homed = ''
        self.head = [0., 0., 0.]                    # where the head is, whatever Klipper counts
        fileconfig = front.fileconfig
        self.limits = {axis: (fileconfig.getfloat('stepper_' + axis, 'position_min', fallback=0.),
                              fileconfig.getfloat('stepper_' + axis, 'position_max'))
                       for axis in 'xyz' if fileconfig.has_section('stepper_' + axis)}

    def check_move(self, move):
        for index, axis in enumerate('xyz'):
            if not move.axes_d[index]:
                continue
            if axis not in self.homed:
                raise move.move_error('Must home axis first')
            low, high = self.limits[axis]
            if not low <= move.end_pos[index] <= high:
                raise move.move_error()
        for index in range(3):
            self.head[index] += move.axes_d[index]
        self.front.step(self.front.turned(move.axes_d[:3]))

    def set_position(self, newpos, homing_axes=''):
        """A homing puts the head where Klipper counts it; SET_KINEMATIC_POSITION only
        changes the count."""
        axes = ''.join('xyz'[axis] if isinstance(axis, int) else axis for axis in homing_axes)
        if self.front.homing:
            self.head = list(newpos[:3])
        self.homed += ''.join(axis for axis in axes if axis not in self.homed)

    def clear_homing_state(self, axes):
        self.homed = ''.join(axis for axis in self.homed if axis not in axes)

    def get_status(self, eventtime):
        return {'homed_axes': ''.join(axis for axis in 'xyz' if axis in self.homed),
                'axis_minimum': [self.limits.get(axis, (0, 0))[0] for axis in 'xyz'] + [0],
                'axis_maximum': [self.limits.get(axis, (0, 0))[1] for axis in 'xyz'] + [0]}


def load(name: str, path: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def release_modules(source: str, front):
    """Klipper imports its modules by top-level name (gcode, toolhead, extras.tmc2209),
    Kalico as the klippy package: while a printer is built, sys.modules serves the
    release's own, with stubs where the MCU would be. Yields import(name) for a module
    as Klipper names it ('gcode', 'extras.tmc2209', 'kinematics.extruder')."""
    root = os.path.join(SRC, source)
    kalico = source.startswith('kalico')
    prefix = 'klippy.' if kalico else ''
    stepper = types.ModuleType(prefix + 'stepper')
    stepper.parse_step_distance = parse_step_distance
    chelper = types.ModuleType(prefix + 'chelper')
    chelper.get_ffi = lambda: (mock.MagicMock(name='ffi_main'), mock.MagicMock(name='ffi_lib'))
    kinematics_stub = types.ModuleType(prefix + 'kinematics.stub')
    kinematics_stub.load_kinematics = lambda toolhead, config: Kinematics(front, toolhead, config)
    tmc_uart = types.ModuleType(prefix + 'extras.tmc_uart')
    tmc_uart.MCU_TMC_uart = Chip
    bulk_sensor = types.ModuleType(prefix + 'extras.bulk_sensor')
    bulk_sensor.BatchBulkHelper = BulkHelper
    stubs = {'stepper': stepper, 'chelper': chelper, 'mcu': types.ModuleType(prefix + 'mcu'),
             'extras.bus': types.ModuleType(prefix + 'extras.bus'), 'extras.tmc_uart': tmc_uart,
             'extras.bulk_sensor': bulk_sensor}
    for kinematics in ('cartesian', 'corexy', 'hbot', 'corexz', 'limited_corexy',
                       'limited_cartesian'):
        stubs['kinematics.' + kinematics] = kinematics_stub
    packages = ['extras', 'kinematics']
    if kalico:
        mathutil = types.ModuleType('klippy.mathutil')
        mathutil.safe_float = float
        danger = types.ModuleType('klippy.extras.danger_options')
        danger.get_danger_options = DangerOptions
        control_mpc = types.ModuleType('klippy.extras.control_mpc')
        control_mpc.ControlMPC = None
        control_mpc.FILAMENT_TEMP_SRC_AMBIENT = 'ambient'
        control_mpc.FILAMENT_TEMP_SRC_FIXED = 'fixed'
        control_mpc.FILAMENT_TEMP_SRC_SENSOR = 'sensor'
        stubs.update({'mathutil': mathutil, 'extras.danger_options': danger,
                      'extras.control_mpc': control_mpc})
        packages.insert(0, '')
    installed = {}
    for name in packages:
        package = types.ModuleType((prefix + name).rstrip('.'))
        package.__path__ = [root]
        installed[package.__name__] = package
    for name, module in stubs.items():
        installed[prefix + name] = module
    for name, module in installed.items():
        parent, _, child = name.rpartition('.')
        if parent in installed:
            setattr(installed[parent], child, module)
    saved = dict(sys.modules)
    sys.modules.update(installed)

    def import_module(name):
        full = prefix + name
        if full not in sys.modules:
            if '.' in name or kalico:
                importlib.import_module(full)
            else:                               # Klipper's top-level klippy/<name>.py
                load(full, os.path.join(root, name + '.py'))
        return sys.modules[full]
    try:
        yield import_module
    finally:
        # every module the release imported goes, its own imports too: the next release
        # must not find them
        for name in list(sys.modules):
            if saved.get(name) is not sys.modules[name]:
                if name in saved:
                    sys.modules[name] = saved[name]
                elif name.split('.')[0] in KLIPPY_NAMES:
                    del sys.modules[name]


class Printer:
    """klippy's Printer as the modules use it: objects, events, the shutdown state."""

    def __init__(self, front, command_error):
        self.front = front
        self.reactor = Reactor(front)
        mcu = Mcu()
        self.objects = {'mcu': mcu, 'pins': Pins(mcu)}
        self.handlers = {}
        self.shutdowns = []
        self.command_error = command_error
        self.config_error = ConfigError
        self.loaders = {'motion_queuing': MotionQueuing}

    def get_start_args(self):
        return {}

    def get_reactor(self):
        return self.reactor

    def register_event_handler(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def send_event(self, event, *args):
        return [callback(*args) for callback in self.handlers.get(event, [])]

    def is_shutdown(self):
        return bool(self.shutdowns)

    def wait_while(self, condition, error_on_cancel=True):     # Kalico
        eventtime = self.reactor.monotonic()
        while condition(eventtime):
            eventtime = self.reactor.pause(eventtime + 1.)

    def invoke_shutdown(self, message, *args):
        if not self.shutdowns:
            self.shutdowns.append(message)
            self.send_event('klippy:shutdown')

    def get_state_message(self):
        if self.shutdowns:
            return self.shutdowns[0] + '\nOnce the underlying issue is corrected, use the ' \
                '"FIRMWARE_RESTART" command to reset the firmware', 'shutdown'
        return 'Printer is ready', 'ready'

    def set_rollover_info(self, *args, **kwargs):
        pass

    def lookup_object(self, name, default=ConfigError):
        if name in self.objects:
            return self.objects[name]
        if default is ConfigError:
            raise ConfigError("Unknown config object '%s'" % name)
        return default

    def lookup_objects(self, module=None):
        return [(name, obj) for name, obj in self.objects.items()
                if module is None or name == module or name.startswith(module + ' ')]

    def load_object(self, config, section, default=ConfigError):
        """The object of a section, made on first ask as klippy does: the extras module's
        load_config (REAL_EXTRAS), or a stand-in."""
        if section not in self.objects:
            if section in self.loaders:
                self.objects[section] = self.loaders[section]()
            elif section in REAL_EXTRAS:
                module = self.front.import_module('extras.' + section)
                self.objects[section] = module.load_config(Config(self.front, section))
            else:
                self.objects[section] = types.SimpleNamespace()
        return self.objects[section]


class Configfile:
    """The configfile status: the config as written, the settings as read."""

    def __init__(self, front):
        self.front = front

    def get_status(self, eventtime):
        fileconfig = self.front.fileconfig
        settings = {}
        for section in fileconfig.sections():
            settings[section.lower()] = {option: option_value(fileconfig.get(section, option))
                                         for option in fileconfig.options(section)}
        for (section, option), value in self.front.tracking.items():
            settings.setdefault(section, {})[option] = value
        return {'config': {section: dict(fileconfig.items(section))
                           for section in fileconfig.sections()},
                'settings': settings, 'warnings': [], 'save_config_pending': False}


class Front:
    """The printer of `source` (a directory in tests/.klipper-src) with `printer_cfg`.
    scripts: every script run; console: every line Klipper printed; moves: each
    FORCE_MOVE and what the chips held while it ran; head_moves: each move the toolhead
    queued (G0/G1), the same way; chips: the TMC chip of each section; crashes: what
    failed in this stand-in itself. A section with no module here (an accelerometer,
    [resonance_tester]) is config only."""

    @staticmethod
    def shake(speed: float, chopper: dict) -> float:
        """How hard a motor shakes the head, in mg, with its belt at `speed`: a resonance
        at 60 mm/s, and of the chopper toff 4, hend 3 the quietest."""
        return ((40. + 600. * math.exp(-((speed - 60.) / 10.) ** 2))
                * (1. + .1 * abs(chopper.get('toff', 4) - 4) + .05 * abs(chopper.get('hend', 3) - 3)))

    @staticmethod
    def chopper(move: dict, stepper: str) -> dict:
        """What the driver of `stepper` held during a move ({} without one)."""
        return move['chips'].get(next((section for section in move['chips']
                                       if section.endswith(' ' + stepper)), ''), {})

    def vibration(self, move: dict) -> float:
        """How hard the head shakes, in mg, during a FORCE_MOVE: its motor alone."""
        return self.shake(move['speed'], self.chopper(move, move['stepper']))

    def head_vibration(self, move: dict) -> float:
        """How hard the head shakes, in mg, during a toolhead move: each X/Y rail whose
        belt runs, as its motors do on the average (a twin left on other registers
        shows); standstill noise when neither runs."""
        mg = 0.
        for axis in 'xy':
            motors = self.rail(axis)
            if move['belts'][axis] > MOVING and motors:
                mg += sum(self.shake(move['belts'][axis], self.chopper(move, motor))
                          for motor in motors) / len(motors)
        return mg or STANDSTILL_MG

    def samples(self, start: float, end: float) -> list:
        """The accelerometer's samples over [start, end): standstill noise, a move shaking
        the head as hard as vibration() or head_vibration() says."""
        out = []
        moves = [(move['window'], self.vibration(move)) for move in self.moves
                 if move['window'][1] > start and move['window'][0] < end]
        moves += [(move['window'], self.head_vibration(move)) for move in self.head_moves
                  if move['window'][1] > start and move['window'][0] < end]
        count = int((end - start) * SAMPLE_HZ)
        for i in range(count):
            t = start + i / SAMPLE_HZ
            mg = next((mg for (low, high), mg in moves if low <= t <= high), STANDSTILL_MG)
            out.append([t, mg * math.sin(i * 0.7), mg * math.cos(i * 1.3), 980.])
        return out

    @staticmethod
    def sources() -> 'list[str | None]':
        """The fetched releases the printer is built from."""
        found = sorted(name for name in os.listdir(SRC) if os.path.isfile(
            os.path.join(SRC, name, 'stepper_enable.py'))) if os.path.isdir(SRC) else []
        return found or [None]

    def __init__(self, source: str, printer_cfg: str):
        self.source = source
        self.fileconfig = configparser.RawConfigParser(strict=False,
                                                       inline_comment_prefixes=(';', '#'))
        self.fileconfig.read_string(printer_cfg)
        self.tracking = {}
        self.homing = False
        self.console, self.moves, self.head_moves, self.chips = [], [], [], {}
        self.steppers, self.sensors = {}, []
        self.crashes, self.scripts, self.listeners = [], [], []
        with release_modules(source, self) as import_module:
            self.import_module = import_module
            gcode = import_module('gcode')
            self.printer = printer = Printer(self, gcode.CommandError)
            self.gcode = gcode.GCodeDispatch(printer)
            printer.objects['gcode'] = self.gcode
            self.gcode.register_output_handler(self.console.append)
            self.gcode.register_output_handler(
                lambda line: [listener(line) for listener in list(self.listeners)])
            printer.objects['configfile'] = Configfile(self)
            toolhead = import_module('toolhead')
            self.toolhead = toolhead.ToolHead(Config(self, 'printer'))
            printer.objects['toolhead'] = self.toolhead
            if hasattr(toolhead, 'ToolHeadCommandHelper'):      # master: the commands live there
                toolhead.ToolHeadCommandHelper(Config(self, 'printer'))
            # as the release builds them from printer.cfg: display_status for [display_status]
            # or [display], respond for [respond] (Kalico: always)
            has = self.fileconfig.has_section
            for name in ('gcode_move', 'stepper_enable', 'force_move') + (
                    ('display_status',) if has('display_status') or has('display') else ()) + (
                    ('respond',) if has('respond') or source.startswith('kalico') else ()):
                printer.load_object(Config(self, name), name)
            printer.objects['force_move'].manual_move = self.manual_move
            self.calc_move_time = import_module('extras.force_move').calc_move_time
            self.gcode.register_command('G28', self.cmd_G28)
            for section in self.fileconfig.sections():
                if section.startswith(STEPPER_SECTIONS):
                    self.add_stepper(section)
            heaters = printer.load_object(Config(self, 'heaters'), 'heaters')
            heaters.have_load_sensors = True            # temperature_sensors.cfg: not here
            heaters.add_sensor_factory('thermistor', Thermistor)
            for section in self.fileconfig.sections():
                if self.fileconfig.has_option(section, 'heater_pin'):
                    # an extruder's heater: PrinterExtruder sets it up under its name
                    heater = heaters.setup_heater(Config(self, section), 'T0')
                    printer.objects.setdefault(section, heater)
            # the SPI transports the drivers share (tmc5160 and tmc2240 take tmc2130's)
            import_module('extras.tmc2130').MCU_TMC_SPI = Chip
            import_module('extras.tmc2660').MCU_TMC2660_SPI = Chip
            for section in self.fileconfig.sections():
                kind = section.split()[0]
                if kind in TMC_SECTIONS:
                    module = import_module('extras.' + kind)
                    printer.objects[section] = module.load_config_prefix(Config(self, section))
            printer.send_event('klippy:mcu_identify')
            printer.send_event('klippy:connect')
            printer.send_event('klippy:ready')
            printer.reactor.run_callbacks()
        del self.import_module
        self.toolhead.trapq_append = self.trapq_append

    def heat(self, eventtime: float):
        """A second passes: each heater is at its target (off: the room's 25 C)."""
        for sensor in self.sensors:
            heater = sensor.callback.__self__
            sensor.callback(eventtime, heater.target_temp or 25.)

    def add_stepper(self, section: str):
        """What stepper.PrinterStepper registers a stepper with: stepper_enable, force_move."""
        stepper = self.steppers[section] = Stepper(section)
        config = Config(self, section)
        config.get('step_pin')
        config.get('dir_pin')
        for name in ('stepper_enable', 'force_move'):
            self.printer.objects[name].register_stepper(config, stepper)

    def rail(self, axis: str) -> 'list[str]':
        """The steppers of an axis's rail: stepper_x and its twins (stepper_x1...)."""
        return [name for name in self.steppers
                if name.startswith('stepper_') and name[8:9] == axis]

    def belts(self, head) -> dict:
        """How far (or how fast) each rail's belt runs for the head's x, y, z: CoreXY's X
        rail runs x+y, its Y rail x-y (corexy.py)."""
        x, y, z = head
        if self.fileconfig.get('printer', 'kinematics').endswith(('corexy', 'hbot')):
            return {'x': abs(x + y), 'y': abs(x - y), 'z': abs(z)}
        return {'x': abs(x), 'y': abs(y), 'z': abs(z)}

    def turned(self, head) -> 'list[str]':
        """The rails a head move of x, y, z turns."""
        return [axis for axis, run in self.belts(head).items() if run > MOVING]

    def step(self, rails):
        """The motors of each rail step; an enable that caused has rewritten the driver by
        the time the move runs."""
        for name in sorted(name for axis in rails for name in self.rail(axis)):
            self.steppers[name].step(self.toolhead.print_time)
        self.printer.reactor.run_callbacks()

    def trapq_append(self, trapq, print_time, accel_t, cruise_t, decel_t, start_x, start_y,
                     start_z, axes_r_x, axes_r_y, axes_r_z, start_v, cruise_v, accel):
        """The toolhead queues a move as the release planned it: its window and cruise,
        where it starts and ends, the head's speed and accel, each rail's belt speed, and
        what the chips held while it ran."""
        start, direction = (start_x, start_y, start_z), (axes_r_x, axes_r_y, axes_r_z)
        end_v = cruise_v - accel * decel_t
        distance = ((start_v + cruise_v) * accel_t / 2 + cruise_v * cruise_t
                    + (cruise_v + end_v) * decel_t / 2)
        self.head_moves.append({
            'start': start, 'end': tuple(s + r * distance for s, r in zip(start, direction)),
            'speed': cruise_v, 'accel': accel,
            'belts': self.belts([r * cruise_v for r in direction]),
            'window': (print_time, print_time + accel_t + cruise_t + decel_t),
            'cruise': (print_time + accel_t, print_time + accel_t + cruise_t),
            'chips': {section: chip.chopper() for section, chip in self.chips.items()}})

    def manual_move(self, stepper, dist, speed, accel=0.):
        """force_move's own manual_move stepped the motor through the step generation;
        each move keeps what the chips held while it ran."""
        _, accel_t, cruise_t, _ = self.calc_move_time(dist, speed, accel)
        start = self.toolhead.get_last_move_time()
        stepper.step(start)
        # the motor starts a buffer time later: an enable it caused has rewritten the
        # driver by then (on a stepper without its own enable pin toff comes back)
        self.printer.reactor.run_callbacks()
        self.moves.append({'stepper': stepper.get_name(), 'distance': dist, 'speed': speed,
                           'accel': accel, 'window': (start, start + 2 * accel_t + cruise_t),
                           'chips': {section: chip.chopper() for section, chip in self.chips.items()}})
        self.toolhead.dwell(2 * accel_t + cruise_t)

    def cmd_G28(self, gcmd):
        """Homing as the tools meet it: each axis named (all without one) on its
        position_endstop, its steppers stepped, homed."""
        axes = [axis for axis in 'XYZ' if gcmd.get(axis, None) is not None] or list('XYZ')
        position = self.toolhead.get_position()
        for axis in axes:
            position['XYZ'.index(axis)] = self.fileconfig.getfloat(
                'stepper_' + axis.lower(), 'position_endstop',
                fallback=self.fileconfig.getfloat('stepper_' + axis.lower(), 'position_min', fallback=0.))
        self.step({rail for axis in axes
                   for rail in self.turned([float(axis == name) for name in 'XYZ'])})
        self.homing = True
        try:
            self.toolhead.set_position(position, homing_axes=''.join(axis.lower() for axis in axes))
        finally:
            self.homing = False

    def endstops(self) -> dict:
        """query_endstops/status: each X/Y/Z endstop, named as the release's stepper.py
        names it (the short 'x' where the rail registers stepper.get_name(short=True)),
        triggered with the head at or past it."""
        with open(os.path.join(SRC, self.source, 'stepper.py')) as module:
            tree = ast.parse(module.read())
        short = any(isinstance(node, ast.FunctionDef) and 'register_endstop' in ast.dump(node)
                    and 'short' in ast.dump(node) for node in ast.walk(tree))
        kinematics = self.toolhead.get_kinematics()
        out = {}
        for index, axis in enumerate('xyz'):
            section = 'stepper_' + axis
            if not self.fileconfig.has_option(section, 'endstop_pin'):
                continue
            endstop = self.fileconfig.getfloat(section, 'position_endstop')
            low, high = kinematics.limits[axis]
            at_max = endstop > (low + high) / 2
            head = kinematics.head[index]
            hit = head >= endstop - 1e-6 if at_max else head <= endstop + 1e-6
            out[axis if short else section] = 'TRIGGERED' if hit else 'open'
        return out

    def run(self, script: str) -> 'str | None':
        """A gcode/script request: None, or the error the API server answers."""
        self.scripts.append(script)
        try:
            self.gcode.run_script(script)
            return None
        except self.printer.command_error as why:
            return str(why)
        finally:
            self.printer.reactor.run_callbacks()
            self.printer.reactor.poll_drivers()

    def status(self, objects: dict) -> dict:
        """objects/query: the fields asked for of each object there is."""
        out = {}
        for name, fields in objects.items():
            obj = self.printer.lookup_object(name, None)
            if obj is None or not hasattr(obj, 'get_status'):
                continue
            status = obj.get_status(0.)
            out[name] = {field: value for field, value in status.items()
                         if fields is None or field in fields}
        return out

    def connect(self) -> Klippy:
        """Our Klippy client on a socket pair, this printer serving the API requests."""
        client, server = socket.socketpair()
        threading.Thread(target=self.serve, args=(server,), daemon=True).start()
        return Klippy('<front>', timeout=10.0).connect(sock=client)

    def serve(self, sock):
        lock = threading.Lock()
        output, subscriptions, streams = [], {}, {}
        sampled = [0.]

        def send(message):
            with lock:
                try:
                    sock.sendall(json.dumps(message).encode() + b'\x03')
                except OSError:                     # a client gone: Klipper drops it too
                    pass

        def listener(line):
            for template in output:
                send(dict(template, params={'response': line}))
        self.listeners.append(listener)
        try:
            with sock:
                self.serve_requests(sock, send, output, subscriptions, streams, sampled)
        finally:
            self.listeners.remove(listener)

    def serve_requests(self, sock, send, output, subscriptions, streams, sampled):
        buffer = b''
        while True:
            try:
                chunk = sock.recv(65536)
            except OSError:
                return
            if not chunk:
                return
            buffer += chunk
            while b'\x03' in buffer:
                raw, buffer = buffer.split(b'\x03', 1)
                request = json.loads(raw)
                try:
                    result, error = self.answer(request, send, output, subscriptions, streams,
                                                sampled)
                except Exception as why:            # a bug of this stand-in, not Klipper's answer
                    self.crashes.append(why)
                    result, error = {}, 'stand-in failed: %r' % why
                if error is None:
                    send({'id': request['id'], 'result': result})
                else:
                    send({'id': request['id'], 'error': {'error': 'WebRequestError', 'message': error}})

    def answer(self, request, send, output, subscriptions, streams, sampled):
        """(result, error) of one API request."""
        method, params = request['method'], request.get('params', {})
        result, error = {}, None
        self.printer.reactor.poll_drivers()         # what Klipper polled while the tool waited
        if method == 'gcode/script':
            error = self.run(params['script'])
            for template in subscriptions.values():
                send(dict(template, params={'eventtime': 0., 'status': self.status(
                    template['objects'])}))
            if streams:
                data = self.samples(sampled[0], self.toolhead.print_time + 0.1)
                if data:
                    sampled[0] = data[-1][0] + 1 / SAMPLE_HZ
                    for template in streams.values():
                        send(dict(template, params={'data': data, 'overflows': 0}))
        elif method == 'gcode/subscribe_output':
            output.append(params['response_template'])
        elif method in ('objects/query', 'objects/subscribe'):
            result = {'eventtime': 0., 'status': self.status(params['objects'])}
            if method == 'objects/subscribe':
                subscriptions[request['id']] = dict(params['response_template'],
                                                    objects=params['objects'])
        elif method == 'query_endstops/status':
            result = self.endstops()
        elif method == 'objects/list':
            result = {'objects': list(self.printer.objects)}
        elif method == 'info':
            message, state = self.printer.get_state_message()
            result = {'state': state, 'state_message': message,
                      'klipper_path': os.path.join(SRC, self.source),
                      'process_id': os.getpid(), 'software_version': self.source}
        elif '/dump_' in method:                    # an accelerometer's stream
            sampled[0] = self.toolhead.print_time
            streams[method + str(params.get('sensor'))] = params['response_template']
            result = {'header': ['time', 'x_acceleration', 'y_acceleration',
                                 'z_acceleration']}
        else:
            error = 'No registered endpoint %s' % method
        return result, error


class FrontMoonraker:
    """Moonraker in front of a Front: the files API on its printer.cfg (no includes),
    G-code to the printer, and a RESTART that builds the printer again from the
    printer.cfg uploaded, the release's TMC modules reading the saved driver_* lines."""

    def __init__(self, source: str, printer_cfg: str):
        self.source, self.files = source, {'printer.cfg': printer_cfg}
        self.front = Front(source, printer_cfg)
        self.uploads, self.restarts = [], 0

    def settings(self) -> dict:
        return self.front.status({'configfile': ['settings']})['configfile']['settings']

    def accepted_commands(self) -> 'set[str]':
        return set(self.front.status({'gcode': ['commands']})['gcode']['commands'])

    def is_printing(self) -> bool:
        return False

    def list_config_files(self) -> 'list[str]':
        return list(self.files)

    def download_config(self, name: str) -> str:
        return self.files[name]

    def upload_config(self, name: str, content: str):
        self.uploads.append(name)
        self.files[name] = content

    def gcode(self, script: str):
        if script.strip() == 'RESTART':
            self.restarts += 1
            self.front = Front(self.source, self.files['printer.cfg'])
            return
        error = self.front.run(script)
        if error is not None:
            raise MoonrakerError(error)

    def set_tmc_fields(self, stepper: str, fields: dict):
        self.gcode(tmc.set_fields_script(stepper, fields))
