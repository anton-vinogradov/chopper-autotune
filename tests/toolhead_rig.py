"""The stress strokes of CHOPPER_ENVELOPE and CHOPPER_CURRENT, and a measurement's stroke
on G1, through the release's own motion planner: gcode.py, extras/gcode_move.py,
toolhead.py (M204, SET_VELOCITY_LIMIT, lookahead) and kinematics/extruder.py run as they
are; the MCU and the step generation are stubs, and trapq_append records the trapezoid
the steppers would get (when it starts, its phases, start_v, cruise_v, accel). One
process per release: Klipper's modules are top-level names.

    python tests/toolhead_rig.py <source dir> '<scenario json>'   prints the result json
"""
import contextlib
import importlib.util
import json
import math
import os
import sys
import threading
import types
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))

TRAPQ = []


def trapq_append(trapq, print_time, accel_t, cruise_t, decel_t, sx, sy, sz, rx, ry, rz,
                 start_v, cruise_v, accel):
    length = (start_v * accel_t + accel * accel_t ** 2 / 2 + cruise_v * cruise_t
              + cruise_v * decel_t - accel * decel_t ** 2 / 2)
    TRAPQ.append({'r': (rx, ry), 'start_v': start_v, 'cruise_v': cruise_v, 'accel': accel,
                  'length': length, 't0': print_time, 'accel_t': accel_t, 'cruise_t': cruise_t,
                  'decel_t': decel_t})


class Kinematics:
    """What cartesian and corexy do for a pure X/Y move: nothing but accept it."""

    def __init__(self, toolhead, config):
        pass

    def check_move(self, move):
        assert not move.axes_d[2]

    def set_position(self, newpos, homing_axes=()):
        pass

    def get_status(self, eventtime):
        return {}

    def clear_homing_state(self, *axes):
        pass


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def load_release(source):
    ffi_lib = mock.MagicMock(name='ffi_lib')
    ffi_lib.trapq_append = trapq_append
    chelper = types.ModuleType('chelper')
    chelper.get_ffi = lambda: (mock.MagicMock(name='ffi_main'), ffi_lib)
    kinematics = types.ModuleType('kinematics_stub')
    kinematics.load_kinematics = Kinematics
    if os.path.basename(source).startswith('kalico'):
        # Kalico's klippy is a package: relative imports, mathutil and danger_options
        package = types.ModuleType('klippy')
        package.__path__ = [source]
        mathutil = types.ModuleType('klippy.mathutil')
        mathutil.safe_float = float
        extras = types.ModuleType('klippy.extras')
        extras.__path__ = []
        danger = types.ModuleType('klippy.extras.danger_options')
        danger.get_danger_options = lambda: DangerOptions()
        kinpackage = types.ModuleType('klippy.kinematics')
        kinpackage.__path__ = []
        for name, module in (('klippy', package), ('klippy.chelper', chelper),
                             ('klippy.stepper', mock.MagicMock(name='stepper')),
                             ('klippy.mathutil', mathutil), ('klippy.extras', extras),
                             ('klippy.extras.danger_options', danger),
                             ('klippy.kinematics', kinpackage),
                             ('klippy.kinematics.cartesian', kinematics),
                             ('klippy.kinematics.corexy', kinematics)):
            sys.modules[name] = module
        package.chelper, package.stepper = chelper, sys.modules['klippy.stepper']
        kinpackage.extruder = load('klippy.kinematics.extruder', os.path.join(source, 'extruder.py'))
        return (load('klippy.toolhead', os.path.join(source, 'toolhead.py')),
                load('klippy.gcode', os.path.join(source, 'gcode.py')),
                load('klippy.extras.gcode_move', os.path.join(source, 'gcode_move.py')),
                load('klippy.extras.force_move', os.path.join(source, 'force_move.py')))
    kinpackage = types.ModuleType('kinematics')
    kinpackage.__path__ = []
    for name, module in (('chelper', chelper), ('stepper', mock.MagicMock(name='stepper')),
                         ('mcu', mock.MagicMock(name='mcu')), ('kinematics', kinpackage),
                         ('kinematics.cartesian', kinematics), ('kinematics.corexy', kinematics)):
        sys.modules[name] = module
    kinpackage.extruder = load('kinematics.extruder', os.path.join(source, 'extruder.py'))
    return (load('toolhead', os.path.join(source, 'toolhead.py')),
            load('gcode', os.path.join(source, 'gcode.py')),
            load('gcode_move', os.path.join(source, 'gcode_move.py')),
            load('force_move', os.path.join(source, 'force_move.py')))


class DangerOptions:
    """Kalico's danger_options at their defaults: times zero, switches off."""

    def __getattr__(self, name):
        return 0. if name.endswith('time') else False


class ConfigError(Exception):
    pass


class Config:
    error = ConfigError
    required = object()

    def __init__(self, printer, name, options):
        self.printer, self.name, self.options = printer, name, options

    def get_printer(self):
        return self.printer

    def get_name(self):
        return self.name

    def has_section(self, name):
        return False

    def deprecate(self, *args, **kwargs):
        pass

    def get(self, option, default=required, **limits):
        if option in self.options:
            return self.options[option]
        if default is self.required:
            raise ConfigError('option %s missing' % option)
        return default

    def getfloat(self, option, default=required, **limits):
        value = self.get(option, default)
        return None if value is None else float(value)

    def getint(self, option, default=required, **limits):
        value = self.get(option, default)
        return None if value is None else int(value)

    def getboolean(self, option, default=required, **limits):
        return bool(self.get(option, default))

    def getchoice(self, option, choices, default=required):
        return choices[self.get(option, default)]


class Reactor:
    NOW = 0.
    NEVER = 9e15

    def monotonic(self):
        return 0.

    def pause(self, waketime):
        return waketime

    def register_timer(self, *args, **kwargs):
        return mock.MagicMock()

    def update_timer(self, *args, **kwargs):
        pass

    def unregister_timer(self, *args, **kwargs):
        pass

    def register_callback(self, *args, **kwargs):
        pass

    def register_async_callback(self, *args, **kwargs):
        pass

    def mutex(self):
        return threading.Lock()

    def assert_no_pause(self):
        return contextlib.nullcontext()


class Mcu:
    def is_fileoutput(self):
        return True

    def estimated_print_time(self, eventtime):
        return 0.

    def __getattr__(self, name):
        return mock.MagicMock(name='mcu.' + name)


class MotionQueuing:                                    # Klipper master only
    def allocate_trapq(self):
        return mock.MagicMock()

    def lookup_trapq_append(self):
        return trapq_append

    def calc_step_gen_restart(self, est):
        return 0.

    def get_kin_flush_delay(self):
        return 0.001

    def __getattr__(self, name):
        return mock.MagicMock(name='motion_queuing.' + name)


class Printer:
    def __init__(self, gcode_module):
        self.reactor = Reactor()
        self.objects = {'mcu': Mcu(), 'pins': types.SimpleNamespace(error=ConfigError)}
        self.handlers = {}
        self.command_error = gcode_module.CommandError
        self.config_error = ConfigError

    def get_start_args(self):
        return {}

    def get_reactor(self):
        return self.reactor

    def register_event_handler(self, event, callback):
        self.handlers.setdefault(event, []).append(callback)

    def send_event(self, event, *args):
        return [callback(*args) for callback in self.handlers.get(event, [])]

    def is_shutdown(self):
        return False

    def set_rollover_info(self, *args, **kwargs):
        pass

    def invoke_shutdown(self, message):
        raise RuntimeError('shutdown: %s' % message)

    def lookup_object(self, name, default=ConfigError):
        if name in self.objects:
            return self.objects[name]
        if default is ConfigError:
            raise ConfigError(name)
        return default

    def lookup_objects(self, module=None):
        return [(name, obj) for name, obj in self.objects.items()
                if module is None or name == module or name.startswith(module + ' ')]

    def load_object(self, config, name, default=None):
        if name == 'motion_queuing':
            return self.objects.setdefault(name, MotionQueuing())
        return self.objects.setdefault(name, mock.MagicMock(name=name))

    def add_object(self, name, obj):
        self.objects[name] = obj


class RigKlippy:
    """The tool's side: G-code into the release's dispatcher, the status out of the
    objects' own get_status."""

    def __init__(self, dispatch, printer, settings):
        self.dispatch, self.printer, self._settings = dispatch, printer, settings

    def gcode(self, script):
        self.dispatch.run_script(script)

    def request(self, method, params=None):
        assert method == 'objects/query'                # the fields asked for, as webhooks does
        return {'status': {name: {field: value
                                  for field, value in self.printer.objects[name].get_status(0.).items()
                                  if fields is None or field in fields}
                           for name, fields in params['objects'].items()}}

    def settings(self):
        return self._settings


def belt_speed(record, kinematics, motor, key='cruise_v'):
    """The belt speed (or accel) of `motor` during a move: corexy's belts run along x+y
    and x-y."""
    rx, ry = record['r']
    if kinematics == 'corexy':
        return abs(rx + ry if motor == 'x' else rx - ry) * record[key]
    return abs(rx if motor == 'x' else ry) * record[key]


def g1_stroke(kl, force_move, kinematics, motor, vec, speed, scenario) -> dict:
    """A measurement's stroke on G1, in the belt's mm as FORCE_MOVE takes them: the head on
    the stroke's start, M400, M204 and the belt's travel along vec, M400. What the planner
    queued against FORCE_MOVE's trapezoid (calc_move_time) of the same travel, and where
    the window the tools cut (steady_window) lies in the stroke's cruise."""
    from chopper_autotune.collect import steady_window, travel_for
    accel, measure_time = scenario['accel'], scenario['measure_time']
    factor = math.hypot(*vec)                           # belt mm per head mm along vec
    travel = travel_for(speed, accel, measure_time)
    start = (10., 10.)
    end = [at + part * travel / factor ** 2 for at, part in zip(start, vec)]
    kl.gcode('G90\nG1 X%.4f Y%.4f F6000\nM400' % start)
    kl.gcode('M204 S%.4f\nG1 X%.4f Y%.4f F%.4f\nM400'
             % (accel / factor, end[0], end[1], speed / factor * 60))
    t_end = kl.request('objects/query', {'objects': {'toolhead': ['print_time']}})[
        'status']['toolhead']['print_time']
    stroke = TRAPQ[-1]
    cruise = stroke['t0'] + stroke['accel_t'], stroke['t0'] + stroke['accel_t'] + stroke['cruise_t']
    low, high = steady_window(t_end, speed, accel, measure_time, scenario['trim'])
    _, accel_t, cruise_t, top = force_move.calc_move_time(travel, speed, accel)
    return {'speed': belt_speed(stroke, kinematics, motor),
            'other': belt_speed(stroke, kinematics, 'y' if motor == 'x' else 'x'),
            'accel': belt_speed(stroke, kinematics, motor, 'accel'), 'start_v': stroke['start_v'],
            'accel_t': stroke['accel_t'], 'cruise_t': stroke['cruise_t'],
            'decel_t': stroke['decel_t'], 'force_move': [accel_t, cruise_t, top],
            'end': cruise[1] + stroke['decel_t'] - t_end,
            'window': [low - cruise[0], cruise[1] - high]}


def run(source, scenario):
    toolhead_module, gcode_module, gcode_move_module, force_move = load_release(source)
    printer = Printer(gcode_module)
    dispatch = gcode_module.GCodeDispatch(printer)
    printer.objects['gcode'] = dispatch
    options = {'max_velocity': scenario['max_velocity'], 'max_accel': scenario['max_accel'],
               'kinematics': scenario['kinematics']}
    config = Config(printer, 'printer', options)
    toolhead = toolhead_module.ToolHead(config)
    printer.objects['toolhead'] = toolhead
    if hasattr(toolhead_module, 'ToolHeadCommandHelper'):  # master: the commands live there
        toolhead_module.ToolHeadCommandHelper(config)
    printer.objects['gcode_move'] = gcode_move_module.GCodeMove(Config(printer, 'gcode_move', {}))
    dispatch.register_command('G28', lambda gcmd: toolhead.set_position([0., 0., 10., 0.]))
    dispatch.register_command('SET_TMC_CURRENT', lambda gcmd: None)
    printer.send_event('klippy:ready')
    toolhead.set_position([0., 0., 10., 0.])
    printer.send_event('toolhead:set_position')

    from chopper_autotune import current, envelope
    current.home_xy = lambda kl, script: kl.gcode(script)
    kl = RigKlippy(dispatch, printer, {'printer': options})
    for script in scenario.get('before', []):
        kl.gcode(script)                                # the state a print may leave behind
    board = types.SimpleNamespace(center=(scenario['center'], scenario['center']),
                                  max_accel=scenario['max_accel'])
    motor, kinematics = scenario['motor'], scenario['kinematics']
    vec = current.stress_vector(kinematics, motor)
    limits = current.live_limits(kl)
    restores = []
    if scenario.get('free', True):
        current.free_strokes(kl, {}, limits, restores)
    freed = current.live_limits(kl)
    rungs = []

    def stroke_records():
        return [{'speed': belt_speed(record, kinematics, motor), 'length': record['length'],
                 'accel': record['accel']} for record in TRAPQ[1:-1]]  # between the end moves
    if scenario['tool'] == 'envelope':
        for speed, accel in scenario['rungs']:
            del TRAPQ[:]
            envelope.stress_burst(kl, board, motor, vec, speed, accel, scenario['span'])
            rungs.append({'speed': speed, 'accel': accel, 'strokes': stroke_records()})
    elif scenario['tool'] == 'g1':
        rungs = [g1_stroke(kl, force_move, kinematics, motor, vec, speed, scenario)
                 for speed in scenario['speeds']]
    else:
        del TRAPQ[:]
        current.run_rung(kl, board, motor, 0.8, 1.0, vec, scenario['span'], scenario['accel'])
        strokes = stroke_records()
        pairs = len(strokes) // len(current.BELT_SPEEDS)
        rungs = [{'speed': speed, 'accel': scenario['accel'],
                  'strokes': strokes[index * pairs:(index + 1) * pairs]}
                 for index, speed in enumerate(current.BELT_SPEEDS)]
    for step in restores:
        step()
    return {'limits': limits, 'freed': freed, 'restored': current.live_limits(kl), 'rungs': rungs}


if __name__ == '__main__':
    print(json.dumps(run(sys.argv[1], json.loads(sys.argv[2]))))
