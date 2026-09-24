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

    def stepper_states(self):
        return {'stepper_x': True, 'stepper_y': True, 'stepper_z': True, 'extruder': False}

    def homed_axes(self):
        return 'xyz'

    def info(self):
        return {}

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


def gantry_released(script: str, cycle: bool = False) -> bool:
    """X/Y off (after an on-off cycle when asked), Z left holding: no M18, no G28."""
    off = ['SET_STEPPER_ENABLE STEPPER="%s" ENABLE=0' % name for name in ('stepper_x', 'stepper_y')]
    cycled = ['SET_STEPPER_ENABLE STEPPER="%s" ENABLE=1\n%s' % (name, line)
              for name, line in zip(('stepper_x', 'stepper_y'), off)]
    return (all(line in script for line in (cycled if cycle else off))
            and 'M18' not in script and 'G28' not in script
            and 'STEPPER="stepper_z" ENABLE=0' not in script)


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
    assert gantry_released(kl.scripts[-1]) and moved(kl) == []


def test_every_move_is_checked_before_it_starts_and_before_a_re_home():
    kl = StatusKl(HOT_X)
    before_move = make_parker(kl, SimpleNamespace(center=(60.0, 60.0), axis_span=120.0))
    with pytest.raises(DriverTooHot):
        before_move(1, 60.0)                    # 60 > headroom 50: this move needs a re-home
    assert moved(kl) == []


def test_a_thermal_stop_releases_the_gantry_instead_of_re_homing():
    # X/Y off and their homing forgotten (FORCE_MOVE left the head elsewhere); Z keeps
    # holding and its homing, so the next run needs no full G28 on a z_hop printer; the
    # on-off cycle catches a driver a register restore re-energized behind Klipper's back
    kl = StatusKl()
    with pytest.raises(DriverTooHot):
        try:
            raise DriverTooHot('hot')
        finally:
            run_restore(lambda: rehome_unless_hot(kl))
    assert len(kl.scripts) == 1 and gantry_released(kl.scripts[0], cycle=True)
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
    assert gantry_released(kl.scripts[-1], cycle=True)


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
    assert gantry_released(kl.scripts[-1], cycle=True)


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


def test_an_impossible_die_temperature_is_no_reading(capsys):
    # #133: with autotune on, ADC_TEMP read 0, which Klipper turns into -265 C
    kl = StatusKl({'tmc2240 stepper_x': {'temperature': -264.7, 'drv_status': {}}})
    guard = ThermalGuard(kl, SETTINGS)
    guard.check()
    guard.check()
    assert capsys.readouterr().out.count('cannot have: the guard watches its flags only') == 1
    kl.status_map['tmc2240 stepper_x']['drv_status'] = {'otpw': 1}   # the flags still count
    with pytest.raises(DriverTooHot):
        guard.check()


def test_the_limit_leaves_a_margin_below_the_shutdowns_of_133():
    # the drivers there shut down at an average die temperature of 111.7 C and above
    assert collect_mod.THERMAL_LIMIT_C <= 100
    kl = StatusKl({'tmc2240 stepper_y': {'temperature': 101.0, 'drv_status': {}}})
    with pytest.raises(DriverTooHot, match='stepper_y overheating'):
        ThermalGuard(kl, SETTINGS).check()


def test_a_tmc2240_without_hold_current_is_named(capsys):
    settings = dict(SETTINGS, **{'tmc2240 stepper_y': {'driver_slope_control': 3, 'run_current': 2.0,
                                                       'hold_current': 1.0}})
    ThermalGuard(StatusKl(settings=settings), settings)
    note = [line for line in capsys.readouterr().out.split('\n') if 'hold the full run_current' in line]
    assert note and 'tmc2240 stepper_x' in note[0] and 'stepper_y' not in note[0]


def test_a_dwell_capture_checks_the_guard_every_second():
    from chopper_autotune.collect import capture_stream
    calls = []
    kl = SimpleNamespace(gcode=lambda script: calls.append(script), print_time=lambda: 10.0,
                         wait_for_sample=lambda t: None,
                         samples_between=lambda a, b: [[a + i * 0.001, 0, 0, 0] for i in range(4000)])
    hw = SimpleNamespace(kl=kl)
    capture_stream(hw, 'G4 P2500', 2.3, lambda: calls.append('check'))
    assert calls == ['M400', 'check', 'G4 P1000\nM400', 'check', 'G4 P1000\nM400',
                     'check', 'G4 P500\nM400']
    # without a guard, or for a move, the script runs as one
    calls.clear()
    capture_stream(hw, 'FORCE_MOVE STEPPER=stepper_x DISTANCE=10 VELOCITY=50', 0.2,
                   lambda: calls.append('check'))
    assert calls == ['M400', 'FORCE_MOVE STEPPER=stepper_x DISTANCE=10 VELOCITY=50\nM400']


def test_a_run_stops_at_the_first_move_after_klipper_shut_down():
    # #133: the old run retried 95 moves against a Klipper in shutdown
    from chopper_autotune.collect import measure_move
    from chopper_autotune.klippy import KlippyError
    attempts = []
    shutdown = KlippyError("gcode/script failed: TMC 'stepper_x' reports error: GSTAT:      "
                           "00000002 drv_err=1(ErrorShutdown!)\nOnce the underlying issue is "
                           "corrected, use the\n\"FIRMWARE_RESTART\" command\nPrinter is shutdown")

    def before_move(direction, travel):
        attempts.append(direction)
        raise shutdown
    hw = SimpleNamespace(stepper='stepper_x', kl=None)
    args = SimpleNamespace(csv=False, trim=0.1, no_raw=True)
    ds = SimpleNamespace(append=lambda record: pytest.fail('no record after a shutdown'))
    with pytest.raises(SystemExit, match=r"Klipper shut down \(TMC 'stepper_x' reports error: "
                                         r"GSTAT: 00000002 drv_err=1\(ErrorShutdown!\)\)"):
        measure_move(hw, ds, args, {'id': 'v026_i0_rev'}, 26.0, 1.0, 50.0, -1, 1000.0, before_move)
    assert attempts == [-1]                                  # no second attempt
