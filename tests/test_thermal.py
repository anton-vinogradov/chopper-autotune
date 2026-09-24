"""The thermal guard (#133): a TMC2240 warned of over-temperature twice, then shut itself
down 4 s later (GSTAT drv_err), taking Klipper with it. Runs now stop on the warning."""
import pytest

from chopper_autotune.collect import (DriverTooHot, ThermalGuard, make_parker, rehome_unless_hot,
                                      run_restore)

SETTINGS = {'stepper_x': {}, 'stepper_y': {}, 'tmc2240 stepper_x': {'driver_slope_control': 3},
            'tmc2240 stepper_y': {'driver_slope_control': 3}, 'tmc2209 extruder': {}}


class StatusKl:
    """Answers objects/query with the given per-section status, records G-code."""

    def __init__(self, status=None, settings=SETTINGS):
        self.status = status or {}
        self._settings = settings
        self.queries = []
        self.scripts = []

    def request(self, method, params):
        self.queries.append(params['objects'])
        return {'status': {name: self.status.get(name, {}) for name in params['objects']}}

    def settings(self):
        return self._settings

    def gcode(self, script):
        self.scripts.append(script)


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
    with pytest.raises(DriverTooHot, match='tmc2240 stepper_y is overheating \\(%s\\)' % why):
        ThermalGuard(kl, SETTINGS).check()


def test_no_driver_sections_means_no_query():
    kl = StatusKl(settings={'stepper_x': {}, 'stepper_y': {}})
    ThermalGuard(kl, kl.settings()).check()
    assert kl.queries == []


def test_every_move_is_checked_before_it_starts():
    from types import SimpleNamespace
    kl = StatusKl({'tmc2240 stepper_x': {'drv_status': {'otpw': 1}}})
    before_move = make_parker(kl, SimpleNamespace(center=(60.0, 60.0), axis_span=120.0))
    with pytest.raises(DriverTooHot):
        before_move(1, 20.0)
    assert kl.scripts == []                      # stopped before the move, no re-home either


def test_a_thermal_stop_turns_the_motors_off_instead_of_re_homing():
    kl = StatusKl()
    with pytest.raises(DriverTooHot):
        try:
            raise DriverTooHot('hot')
        finally:
            run_restore(lambda: rehome_unless_hot(kl))
    assert 'G28' not in kl.scripts[-1] and 'STEPPER=stepper_x ENABLE=0' in kl.scripts[-1]
    kl.scripts.clear()
    rehome_unless_hot(kl)                        # any other ending re-homes as before
    assert kl.scripts == ['G28 X Y']


@pytest.mark.parametrize('extra, advised', [
    ({}, True),                                                    # stock slope 0, no autotune
    ({'autotune_tmc stepper_x': {}, 'autotune_tmc stepper_y': {}}, False),
])
def test_a_tmc2240_on_the_hottest_slope_gets_advice(extra, advised):
    settings = dict(SETTINGS, **{'tmc2240 stepper_x': {'driver_slope_control': 0},
                                 'tmc2240 stepper_y': {}}, **extra)
    guard = ThermalGuard(StatusKl(settings=settings), settings)
    assert bool(guard.advice) is advised
    if advised:
        assert 'tmc2240 stepper_x, tmc2240 stepper_y' in guard.advice


def test_demo_does_not_skip_to_the_next_motor_after_a_thermal_stop(monkeypatch):
    from chopper_autotune import demo as demo_module
    from chopper_autotune.cli import build_parser
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
