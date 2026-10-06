"""Several motors on one axis (AWD, a two-motor gantry, #129): COLLECT, FIND_SPEED and
TUNE plan the axis as a rail (chopper_autotune/rail.py, run on printers in test_rail.py);
the tools that move or tune one motor refuse it before any G-code."""
import math

import pytest

from chopper_autotune import rail
from chopper_autotune.cli import build_parser
from chopper_autotune.collect import rail_twins, refuse_multi_motor, release_gantry

AWD = {'stepper_x': {}, 'stepper_x1': {}, 'stepper_y': {}, 'stepper_y1': {}, 'stepper_z': {}}


class RecordingKl:
    path = '<sock>'

    def __init__(self, settings):
        self._settings = settings
        self.scripts = []

    def settings(self):
        return self._settings

    def gcode(self, script):
        self.scripts.append(script)

    def object_list(self):
        return []

    def info(self):
        return {}

    def stepper_states(self):
        return dict({name: True for name in self._settings if name.startswith('stepper_')},
                    extruder=True)

    def homed_axes(self):
        return 'xyz'

    def connect(self, sock=None):
        return self

    def close(self):
        pass


def test_rail_twins_follows_klippers_rule():
    # klippy/stepper.py LookupMultiRail: stepper_x1, stepper_x2, ... up to the first gap
    settings = {'stepper_x': {}, 'stepper_x1': {}, 'stepper_x2': {}, 'stepper_x4': {}}
    assert rail_twins(settings, 'x') == ['stepper_x1', 'stepper_x2']
    assert rail_twins(settings, 'y') == []


RAIL = {'printer': {'kinematics': 'cartesian', 'max_accel': 500},
        'stepper_x': {'position_min': 0, 'position_max': 300}, 'stepper_x1': {},
        'stepper_y': {'position_min': 0, 'position_max': 210},
        'tmc5160 stepper_x': {'driver_toff': 3, 'driver_hend': 2},
        'tmc5160 stepper_x1': {'driver_toff': 5, 'stealthchop_threshold': 999999},
        'resonance_tester': {'accel_chip': 'adxl345'}}


def test_only_the_rail_query_brings_in_the_twins():
    # the tools that drive stepper_x alone (MAP, DEMO, ENVELOPE, CURRENT, BELTS, EXTRUDER)
    # get no twin; a run asking for the rail reads each twin from its own section, no G-code
    from chopper_autotune.collect import detect_hardware, rail_of
    kl = RecordingKl(RAIL)
    hw = detect_hardware(kl, 'x')
    assert hw.twins == [] and hw.rail == [hw]
    assert rail_of(hw) is hw
    assert [(drive.stepper, drive.driver.name, drive.baseline, bool(drive.stealth))
            for drive in hw.rail] == [('stepper_x', '5160', {'toff': 3, 'hend': 2}, False),
                                      ('stepper_x1', '5160', {'toff': 5}, True)]
    assert kl.scripts == []
    without = dict(RAIL)
    del without['tmc5160 stepper_x1']
    with pytest.raises(SystemExit, match='no supported TMC driver section found for stepper_x1'):
        rail_of(detect_hardware(RecordingKl(without), 'x'))


def rail_settings(kinematics: str = 'cartesian', **sections) -> dict:
    """settings of a printer with two motors on X and on Y, each with a TMC5160."""
    settings = {'printer': {'kinematics': kinematics, 'max_accel': 500, 'max_velocity': 500},
                'resonance_tester': {'accel_chip': 'adxl345'}}
    for axis, size in (('x', 300), ('y', 210)):
        for name in ('stepper_' + axis, 'stepper_%s1' % axis):
            settings[name] = {'rotation_distance': 40, 'microsteps': 16, 'full_steps_per_rotation': 200}
            settings['tmc5160 ' + name] = {'run_current': 0.8}
        settings['stepper_' + axis].update(position_min=0, position_max=size)
    for name, options in sections.items():
        settings[name.replace('__', ' ')] = dict(settings.get(name.replace('__', ' '), {}), **options)
    return settings


def test_the_one_motor_tools_refuse_a_two_motor_axis_and_the_rail_runs_take_it():
    refuse_multi_motor({'stepper_x': {}, 'stepper_y': {}, 'stepper_z': {}, 'stepper_z1': {}})
    with pytest.raises(SystemExit, match=r'not on two-motor axes yet \(#129\): stepper_x1, stepper_y1 '
                                         r'share an axis'):
        refuse_multi_motor(AWD)
    refuse_multi_motor(rail_settings(), rails=True)
    refuse_multi_motor(rail_settings('limited_corexy'), rails=True)


@pytest.mark.parametrize('module, function, argv', [
    ('chopper_autotune.resonance_map', 'resonance_map', ['map', '--dry-run']),
    ('chopper_autotune.current', 'current_tune', ['current', '--dry-run']),
])
def test_the_one_motor_tools_refuse_awd_before_any_gcode(module, function, argv):
    import importlib
    kl = RecordingKl(AWD)
    with pytest.raises(SystemExit, match=r'not on two-motor axes yet \(#129\)'):
        getattr(importlib.import_module(module), function)(kl, build_parser().parse_args(argv))
    assert kl.scripts == []


@pytest.mark.parametrize('module, function, argv', [
    ('chopper_autotune.collect', 'collect', ['collect', '--speed', '55', '--dry-run']),
    ('chopper_autotune.find_speed', 'scan', ['find-speed', '--dry-run']),
])
def test_a_rail_run_refuses_an_unsupported_rail_before_any_gcode(module, function, argv):
    import importlib
    kl = RecordingKl(rail_settings('hybrid_corexy'))
    with pytest.raises(SystemExit, match='not on hybrid_corexy with two motors on an axis'):
        getattr(importlib.import_module(module), function)(kl, build_parser().parse_args(argv))
    assert kl.scripts == []


def _never(*args, **kwargs):
    pytest.fail('the entry point went on to a per-motor step')


@pytest.mark.parametrize('module, function, argv, settings, refusal, next_steps', [
    ('chopper_autotune.tune', 'run_tune', ['tune', '--csv', '--dry-run'], rail_settings(),
     r'drop CSV=1', ('scan', 'collect')),
    ('chopper_autotune.tune', 'run_tune', ['tune', '--dry-run'], rail_settings('hybrid_corexy'),
     r'not on hybrid_corexy', ('scan', 'collect')),
    ('chopper_autotune.demo', 'run_demo', ['demo', '--dry-run'], AWD,
     r'not on two-motor axes yet \(#129\)', ('showcase_together', 'demo')),
    ('chopper_autotune.demo', 'run_demo', ['demo', '--report', '--dry-run'], AWD,
     r'not on two-motor axes yet \(#129\)', ('showcase_together', 'demo')),
])
def test_pipeline_entry_points_refuse_first(monkeypatch, module, function, argv, settings, refusal,
                                            next_steps):
    # refused at the entry itself: TUNE says it before the other motor's tune, demo's
    # per-motor loop would read it as 'motor A skipped'
    import importlib
    target = importlib.import_module(module)
    kl = RecordingKl(settings)
    monkeypatch.setattr(target, 'find_socket', lambda *args: '<sock>')
    monkeypatch.setattr(target, 'Klippy', lambda path: kl)
    for name in next_steps:
        monkeypatch.setattr(target, name, _never)
    with pytest.raises(SystemExit, match=refusal):
        getattr(target, function)(build_parser().parse_args(argv))
    assert kl.scripts == []


def test_only_the_driven_axes_count():
    # a dual-Y gantry can still map or demo X; Y or both are refused there
    dual_y = {'stepper_x': {}, 'stepper_y': {}, 'stepper_y1': {}}
    refuse_multi_motor(dual_y, 'x')
    for axes in ('y', 'xy'):
        with pytest.raises(SystemExit, match='stepper_y1'):
            refuse_multi_motor(dual_y, axes)
    # a rail run checks the rails it drives alone
    odd = rail_settings(stepper_x1={'rotation_distance': 39.64})
    refuse_multi_motor(odd, 'y', rails=True)
    with pytest.raises(SystemExit, match='match rotation_distance on motor A'):
        refuse_multi_motor(odd, 'xy', rails=True)


@pytest.mark.parametrize('sections, refusal', [
    ({'stepper_y1': {'gear_ratio': [[80, 16]]}}, 'match gear_ratio on motor B'),
    ({'stepper_y1': {'full_steps_per_rotation': 400}}, 'match full_steps_per_rotation on motor B'),
    ({'stepper_y': {'gear_ratio': '80:16'}, 'stepper_y1': {'gear_ratio': [[80, 16]]}}, None),
    ({'autotune_tmc__stepper_y1': {'tuning_goal': 'performance'}},
     'put klipper_tmc_autotune on all of stepper_y, stepper_y1 or on none'),
    ({'autotune_tmc__stepper_y': {}, 'autotune_tmc__stepper_y1': {}}, None),
    ({'dual_carriage': {}}, r'not with \[dual_carriage\]'),
])
def test_a_rail_is_refused_where_its_motors_would_run_unlike(sections, refusal):
    settings = rail_settings(**sections)
    if refusal is None:
        rail.refuse_unsupported(settings, 'y')
    else:
        with pytest.raises(SystemExit, match=refusal):
            rail.refuse_unsupported(settings, 'y')


class PlanKl(RecordingKl):
    """A printer a rail plan reads: the settings, and the status of the toolhead and
    gcode_move."""

    def __init__(self, settings, origin=(0.0, 0.0), z=None):
        super().__init__(settings)
        self.origin, self.z = origin, z

    def request(self, method, params=None):
        assert method == 'objects/query'
        printer = self._settings['printer']
        status = {'toolhead': {'homed_axes': 'xyz' if self.z is not None else 'xy',
                               'position': [0, 0, self.z or 0, 0],
                               'max_velocity': printer['max_velocity'],
                               'max_accel': printer['max_accel'],
                               'minimum_cruise_ratio': 0.5},
                  'gcode_move': {'homing_origin': list(self.origin) + [0, 0], 'speed_factor': 1.0}}
        return {'status': {name: status[name] for name in params['objects'] if name in status}}


def planned(settings, axis='x', **kl):
    from chopper_autotune.collect import detect_hardware, rail_of
    printer = PlanKl(settings, **kl)
    hw = rail_of(detect_hardware(printer, axis))
    args = build_parser().parse_args(['collect', '--speed', '60', '--dry-run'])
    return rail.RailMove(printer, hw, args), printer


def test_a_rail_keeps_its_edges_on_each_axis_its_move_runs_along():
    # cartesian: the axis less an edge each side, max(25, a tenth of it); CoreXY: the head
    # runs L/2 along each axis, so twice the shorter axis less its edges
    motion, _ = planned(rail_settings())
    assert (motion.k, motion.limit) == (1, 300 - 2 * 30)
    motion, _ = planned(rail_settings(), 'y')
    assert motion.limit == 210 - 2 * 25
    motion, _ = planned(rail_settings('corexy'), 'y')
    assert motion.k == pytest.approx(math.sqrt(2)) and motion.limit == pytest.approx(2 * (210 - 50))
    assert motion.margin(motion.limit) == pytest.approx(25)


def test_the_moves_run_across_the_bed_center_in_gcode_coordinates():
    # Klipper adds homing_origin to every G1 after a homing (SET_GCODE_OFFSET)
    motion, printer = planned(rail_settings('corexy'), 'y', origin=(5.0, -3.0))
    start, end = motion.ends(100, 1)
    assert start == pytest.approx([150 - 25 - 5, 105 + 25 + 3])
    assert end == pytest.approx([150 + 25 - 5, 105 - 25 + 3])
    assert motion.ends(100, -1) == [end, start]
    assert motion.script(100, 60, 300) == 'G1 X%.3f Y%.3f F%.3f' % (*end, 60 / math.sqrt(2) * 60)
    assert printer.scripts == []


@pytest.mark.parametrize('kinematics, axis, top, cruise, asked, belt', [
    ('cartesian', 'x', 60, 1.25, 50, 50),            # the tools' max_accel/10 fits 147 mm
    ('cartesian', 'x', 120, 1.0, 200, 200),          # the least hundred that fits 240 mm
    ('cartesian', 'y', 120, 1.0, 400, 400),          # 160 mm
    ('cartesian', 'y', 400, 1.0, 500, 500),          # nothing fits: max_accel, the cruise shrinks
    ('corexy', 'y', 400, 1.0, 500 * math.sqrt(2), 500 * math.sqrt(2)),   # the belt's ceiling
])
def test_the_accel_rule_fits_the_top_speed_to_the_bed(kinematics, axis, top, cruise, asked, belt):
    motion, _ = planned(rail_settings(kinematics), axis)
    assert motion.accel(None, top, cruise) == pytest.approx(belt)
    assert motion.asked == pytest.approx(asked)
    assert motion.accel(1234, top, cruise) == 1234   # ACCEL= as given


def test_kalicos_per_axis_limits_cap_the_accel_the_moves_get():
    # a dry run reads them from the config; the run itself asks SET_KINEMATICS_LIMIT
    motion, printer = planned(rail_settings('limited_corexy', printer={'max_x_accel': 300.0,
                                                                       'max_y_accel': 300.0}), 'y')
    assert motion.accel(None, 400, 1.0) == pytest.approx(300)
    assert motion.asked == pytest.approx(500 * math.sqrt(2))
    assert printer.scripts == []


def test_the_plan_names_what_runs_the_drivers_of_a_rail_unlike(capsys):
    # named, not refused: the run sets the chopper registers alone
    from chopper_autotune.collect import travel_for
    settings = rail_settings(**{'tmc5160__stepper_x': {'hold_current': 0.5, 'interpolate': True},
                                'tmc5160__stepper_x1': {'run_current': 0.9, 'hold_current': 0.5,
                                                        'interpolate': False, 'driver_toff': 5},
                                'stepper_x': {'enable_pin': '!P1'}, 'stepper_x1': {'enable_pin': '!P1'},
                                'skew_correction': {}, 'z_thermal_adjust': {}, 'motors_sync': {},
                                'input_shaper': {'shaper_freq_x': 40.0, 'shaper_freq_y': 0.0}})
    motion, printer = planned(settings)
    motion.plan([(60, travel_for(60, motion.accel(None, 60, 1.25), 1.25))])
    out = capsys.readouterr().out
    assert ('WARNING: the drivers of motor A differ in registers (stepper_x '
            'tbl2_toff3_hstrt5_hend2_tpfd4, stepper_x1 tbl2_toff5_hstrt5_hend2_tpfd4); run_current '
            '(stepper_x 0.8, stepper_x1 0.9); '
            'interpolate (stepper_x True, stepper_x1 False)') in out
    assert out.count('shares its enable pin') == 2
    assert 'input shaper (by config X 40 Hz, Y 0 Hz) -> off' in out
    for note in ('[skew_correction] stays on', '[z_thermal_adjust] stays on',
                 '[motors_sync]: run G28 and SYNC_MOTORS', 'Z is not homed'):
        assert 'note: ' + note in out
    assert ('Every X/Y motor holds under current the whole run: stepper_x 0.5 A, stepper_x1 0.5 A, '
            'stepper_y its run_current 0.8 A, stepper_y1 its run_current 0.8 A') in out
    assert 'WARNING: stepper_y, stepper_y1 hold the full run_current at standstill' in out
    assert printer.scripts == []


def test_a_rail_of_three_runs_the_same_way(capsys):
    # proven on two: a third motor (stepper_x2) is one more driver of the same rail
    from chopper_autotune.collect import set_rail_fields
    third = {'stepper_x2': {'rotation_distance': 40, 'microsteps': 16, 'full_steps_per_rotation': 200},
             'tmc5160__stepper_x2': {'run_current': 0.8}}
    motion, printer = planned(rail_settings(**third))
    motion.plan([(60, 147.0)])
    assert 'rail of 3 TMC5160 drivers (stepper_x, stepper_x1, stepper_x2)' in capsys.readouterr().out
    assert motion.manifest_fields()['steppers'] == ['stepper_x', 'stepper_x1', 'stepper_x2']
    set_rail_fields(printer, motion.hw, {'toff': 4})
    assert printer.scripts == ['\n'.join('SET_TMC_FIELD STEPPER=%s FIELD=toff VALUE=4' % name
                                         for name in ('stepper_x', 'stepper_x1', 'stepper_x2'))]
    with pytest.raises(SystemExit, match='match microsteps on motor A'):
        rail.refuse_unsupported(rail_settings(**dict(third, stepper_x2=dict(third['stepper_x2'],
                                                                            microsteps=32))), 'x')


def test_a_rail_run_wants_the_stream_and_room_under_the_nozzle():
    with pytest.raises(SystemExit, match='drop CSV=1'):
        rail.refuse_rail_run(PlanKl(rail_settings()), True)
    with pytest.raises(SystemExit, match=r'raise Z to 5 mm or more \(G1 Z10\), then retry: '
                                         r'.* now at Z 2.0'):
        rail.refuse_rail_run(PlanKl(rail_settings(), z=2.0), False)
    rail.refuse_rail_run(PlanKl(rail_settings(), z=10.0), False)
    rail.refuse_rail_run(PlanKl(rail_settings()), False)      # Z unhomed: a note in the plan


def test_envelope_note_names_the_twins_of_the_measured_motors():
    from chopper_autotune.envelope import awd_note
    assert 'stepper_x1' in awd_note(AWD, ['x']) and 'stepper_y1' not in awd_note(AWD, ['x'])
    assert awd_note({'stepper_x': {}, 'stepper_y': {}}, ['x', 'y']) == ''
    assert '"' not in awd_note(AWD, ['x', 'y'])              # travels inside RESPOND MSG="..."


def test_belt_jog_releases_the_gantry(monkeypatch):
    from types import SimpleNamespace

    from chopper_autotune.belts import identify_belt
    kl = RecordingKl(AWD)
    hw = SimpleNamespace(center=(60.0, 60.0), kinematics='corexy', axis_span=120.0)
    identify_belt(kl, hw, 'x', SimpleNamespace(update=lambda *args, **kwargs: None), cycles=1)
    assert kl.scripts[-1].startswith(XY_OFF) and 'M18' not in kl.scripts[-1]


XY_OFF = '\n'.join('SET_STEPPER_ENABLE STEPPER="%s" ENABLE=0' % name
                   for name in ('stepper_x', 'stepper_x1', 'stepper_y', 'stepper_y1', 'extruder'))


def test_release_gantry_forgets_only_the_xy_homing():
    # hands move the head next; Z keeps holding and its homing ([safe_z_home] z_hop)
    kl = RecordingKl(AWD)
    release_gantry(kl)
    assert kl.scripts == [XY_OFF + '\nSET_KINEMATIC_POSITION SET_HOMED= CLEAR_HOMED=XY']


def test_a_cycled_release_cycles_every_xy_motor_twins_included_one_by_one():
    # as main did, a motor alone on its axis included: Klipper dwells 100 ms around each
    # SET_STEPPER_ENABLE, so no order switches the motors of a rail at one instant
    kl = RecordingKl(AWD)
    release_gantry(kl, cycle=True)
    assert kl.scripts[0].split('\n')[:9] == [
        'SET_STEPPER_ENABLE STEPPER="%s" ENABLE=%d' % (name, state)
        for name in ('stepper_x', 'stepper_x1', 'stepper_y', 'stepper_y1')
        for state in (1, 0)] + ['SET_STEPPER_ENABLE STEPPER="extruder" ENABLE=0']
