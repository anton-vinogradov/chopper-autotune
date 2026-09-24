"""The thermal guard (#133): a TMC2240 warned of over-temperature twice, then shut itself
down 4 s later (GSTAT drv_err), taking Klipper with it. Runs now stop on the warning."""
from types import SimpleNamespace

import pytest

import chopper_autotune.collect as collect_mod
from chopper_autotune import tmc
from chopper_autotune.cli import build_parser
from chopper_autotune.collect import (DriverTooHot, Hardware, ThermalGuard, make_parker,
                                      rehome_unless_hot, run_restore)

SETTINGS = {'stepper_x': {}, 'stepper_y': {},
            'tmc2240 stepper_x': {'driver_slope_control': 3, 'run_current': 1.2},
            'tmc2240 stepper_y': {'driver_slope_control': 3, 'run_current': 1.2},
            'tmc2209 extruder': {}}
HOT_X = {'tmc2240 stepper_x': {'drv_status': {'otpw': 1, 'stst': 1, 'cs_actual': 31}}}


@pytest.fixture(autouse=True)
def no_poll_wait(monkeypatch):
    monkeypatch.setattr(collect_mod, 'PREFLIGHT_SEC', 0)


class StatusKl:
    """Keeps the pushed driver status (status_map) and records G-code."""
    path = '<sock>'

    def __init__(self, status=None, settings=SETTINGS):
        self.status_map = status or {}
        self._settings = settings
        self.queries = []
        self.scripts = []

    def subscribe_status(self, objects):
        self.queries.append(objects)

    def status(self):
        # the live copy Klipper keeps pushing: whatever the driver reports right now
        return {name: dict(self.status_map.get(name, {})) for name in self.queries[-1]}

    def settings(self):
        return self._settings

    def gcode(self, script):
        self.scripts.append(script)

    def gcode_output(self, script):
        return []

    def subscribe_accel(self, chip):
        pass

    def object_list(self):
        return []

    def is_printing(self):
        return False


def moved(kl):
    return [s for s in kl.scripts if s.lstrip().startswith(('G28', 'G1', 'G0', 'FORCE_MOVE'))]


def test_quiet_drivers_pass_and_only_xy_drivers_are_asked():
    kl = StatusKl({'tmc2240 stepper_x': {'drv_status': {'stst': 1}, 'temperature': 58.0},
                   'tmc2240 stepper_y': {'drv_status': None, 'temperature': None}})   # motor off
    ThermalGuard(kl, SETTINGS).check()
    assert set(kl.queries[0]) == {'tmc2240 stepper_x', 'tmc2240 stepper_y'}


@pytest.mark.parametrize('status, why', [
    ({'drv_status': {'otpw': 1, 'stst': 1, 'cs_actual': 31}}, 'otpw'),     # the #133 log line
    ({'drv_status': {'t120': 1}}, 't120'),                                 # TMC2209 flags
    ({'drv_status': {}, 'temperature': 112.4}, '112 C'),                   # TMC2240 die sensor
])
def test_an_over_temperature_warning_stops_the_run(status, why):
    kl = StatusKl({'tmc2240 stepper_y': status})
    with pytest.raises(DriverTooHot, match='tmc2240 stepper_y overheating \\(%s\\)' % why):
        ThermalGuard(kl, SETTINGS).check()


def test_the_stop_message_fits_the_display_with_its_advice():
    # announce_failure shows '<command> FAILED: <message>' cut at 120 characters
    settings = dict(SETTINGS, **{'tmc2240 stepper_x': {}})              # stock slope_control
    with pytest.raises(DriverTooHot) as stop:
        ThermalGuard(StatusKl(HOT_X, settings), settings).check()
    shown = ('envelope FAILED: %s' % stop.value.code)[:120]
    assert 'motors off' in shown and 'driver_SLOPE_CONTROL: 3' in shown and '#133' in shown


def test_no_driver_sections_means_no_query():
    kl = StatusKl(settings={'stepper_x': {}, 'stepper_y': {}})
    guard = ThermalGuard(kl, kl.settings())
    guard.preflight()
    guard.check()
    assert kl.queries == [] and kl.scripts == []


def test_preflight_refuses_a_driver_still_hot_before_any_move():
    # off motors publish no status: the preflight enables them (no motion) and asks
    kl = StatusKl(HOT_X)
    with pytest.raises(DriverTooHot):
        ThermalGuard(kl, SETTINGS).preflight()
    assert kl.scripts[0] == ('SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=1\n'
                             'SET_STEPPER_ENABLE STEPPER=stepper_y ENABLE=1')
    assert kl.scripts[-1] == 'M18' and moved(kl) == []


def test_every_move_is_checked_before_it_starts_and_before_a_re_home():
    kl = StatusKl(HOT_X)
    before_move = make_parker(kl, SimpleNamespace(center=(60.0, 60.0), axis_span=120.0))
    with pytest.raises(DriverTooHot):
        before_move(1, 60.0)                    # 60 > headroom 50: this move needs a re-home
    assert moved(kl) == []


def test_a_thermal_stop_turns_the_motors_off_with_m18_instead_of_re_homing():
    # M18: only motor_off forgets the homing, FORCE_MOVE left the head elsewhere
    kl = StatusKl()
    with pytest.raises(DriverTooHot):
        try:
            raise DriverTooHot('hot')
        finally:
            run_restore(lambda: rehome_unless_hot(kl))
    assert kl.scripts == ['M18']
    kl.scripts.clear()
    rehome_unless_hot(kl)                        # any other ending re-homes as before
    assert kl.scripts == ['G28 X Y']


@pytest.mark.parametrize('extra, advised', [
    ({}, True),                                                    # stock slope 0, no autotune
    ({'autotune_tmc stepper_x': {}, 'autotune_tmc stepper_y': {}}, False),
])
def test_a_tmc2240_on_the_hottest_slope_gets_advice(extra, advised, capsys):
    settings = dict(SETTINGS, **{'tmc2240 stepper_x': {'driver_slope_control': 0},
                                 'tmc2240 stepper_y': {}}, **extra)
    guard = ThermalGuard(StatusKl(settings=settings), settings)
    assert bool(guard.slow) is advised
    assert ('driver_SLOPE_CONTROL: 3' in capsys.readouterr().out) is advised


def hardware(kl):
    return Hardware(kl=kl, stepper='stepper_x', driver=tmc.DRIVERS['2240'], accel_chip='adxl345',
                    kinematics='corexy', axis_span=260, center=(130, 130), max_accel=10000,
                    baseline={'tbl': 0, 'toff': 3, 'hstrt': 5, 'hend': 0})


def test_current_checks_inside_a_rung_and_ends_with_the_motors_off(monkeypatch):
    # a rung is 15-30 s of load: the check runs before every stroke pair, not once per rung
    import chopper_autotune.current as cur
    kl = StatusKl()
    monkeypatch.setattr(cur, 'detect_hardware', lambda kl_, axis, accel=False: hardware(kl_))
    monkeypatch.setattr(cur, 'Referee', lambda *a: SimpleNamespace(calibrate=lambda: None,
                                                                     slipped=lambda: 0.0))
    strokes = []
    real_gcode = kl.gcode

    def gcode(script):
        real_gcode(script)
        if script.count('\nG1 ') == 1 and script.startswith('G1 '):
            strokes.append(script)
            if len(strokes) == 3:                # the driver warns in the middle of the rung
                kl.status_map = HOT_X
    kl.gcode = gcode
    with pytest.raises(DriverTooHot):
        cur.current_tune(kl, build_parser().parse_args(['current', '--motor', 'a', '--yes']))
    assert len(strokes) == 3                     # stopped at the next stroke pair
    assert kl.scripts[-1] == 'M18'


def test_find_speed_ends_with_the_motors_off_after_a_thermal_stop(tmp_path, monkeypatch):
    import chopper_autotune.find_speed as fs
    kl = StatusKl()
    monkeypatch.setattr(fs, 'detect_hardware', lambda kl_, axis: hardware(kl_))
    monkeypatch.setattr(fs, 'measure_baseline', lambda hw_, ds, args, done: None)

    def move(hw_, ds, args, record, speed, cruise, travel, direction, accel, before):
        kl.status_map = HOT_X                        # the driver heats up during the scan
        before(direction, travel)
    monkeypatch.setattr(fs, 'measure_move', move)
    args = build_parser().parse_args(['find-speed', '--motor', 'a', '--yes', '--min-speed', '58',
                                      '--max-speed', '58', '--dataset', str(tmp_path / 'ds'),
                                      '--no-raw'])
    with pytest.raises(DriverTooHot):
        fs.scan(kl, args)
    assert kl.scripts[-1] == 'M18'


def test_envelope_bursts_are_checked_between_passes():
    from chopper_autotune.envelope import stress_burst
    kl = StatusKl()
    checks = []
    board = SimpleNamespace(center=(130.0, 130.0))
    stress_burst(kl, board, 'x', (1.0, 1.0), 100.0, 5000.0, 20.0, lambda: checks.append(1))
    passes = [s for s in kl.scripts if s.startswith('G1 ') and s.count('\nG1 ') == 1]
    assert len(checks) == len(passes) + 1        # before the approach and before every pass


def test_the_demo_show_checks_before_every_sweep_stroke():
    from chopper_autotune.demo import _sweep
    checks = []
    board = SimpleNamespace(kinematics='corexy', center=(130.0, 130.0), kl=StatusKl())
    args = SimpleNamespace(repeats=1)
    with pytest.raises(DriverTooHot):
        def check():
            checks.append(1)
            if len(checks) == 2:
                raise DriverTooHot('hot')
        _sweep(board, {'x': 58, 'y': 34}, 1000, 100.0, args, check)
    assert len(checks) == 2                      # the approach, then the first stroke


def test_demo_does_not_skip_to_the_next_motor_after_a_thermal_stop(monkeypatch):
    from chopper_autotune import demo as demo_module
    played = []

    def hot(kl, args):
        played.append(args.axis)
        raise DriverTooHot('hot')
    kl = type('K', (), {'connect': lambda self: self, 'close': lambda self: None,
                        'settings': lambda self: {}})()
    monkeypatch.setattr(demo_module, 'find_socket', lambda socket: 'sock')
    monkeypatch.setattr(demo_module, 'Klippy', lambda path: kl)
    monkeypatch.setattr(demo_module, 'demo', hot)
    with pytest.raises(DriverTooHot):
        demo_module.run_demo(build_parser().parse_args(['demo', '--report']))
    assert played == ['x']
