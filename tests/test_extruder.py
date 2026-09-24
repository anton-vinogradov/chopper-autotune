import pytest

from chopper_autotune import tmc
from chopper_autotune import extruder as extruder_mod
from chopper_autotune.extruder import (extruder_context, load_winner_state, oscillation,
                                       resonant_speed, save_winner_state)


def test_resonant_speed_picks_the_peak():
    # the measured E0 curve: 5 mm/s rang 3x above the neighbours
    curve = [(1, 886), (2, 872), (3, 733), (5, 2211), (8, 643), (12, 628)]
    assert resonant_speed(curve) == 5


def test_oscillation_is_net_zero():
    script = oscillation(5.0, 2.5, cycles=3)
    lines = script.splitlines()
    assert len(lines) == 6
    forward = sum(1 for l in lines if 'DISTANCE=2.50' in l)
    back = sum(1 for l in lines if 'DISTANCE=-2.50' in l)
    assert forward == back == 3                     # never a net feed or retract
    assert all('STEPPER=extruder' in l for l in lines)


def test_extruder_context_reads_the_tmc_section():
    settings = {
        'tmc2209 extruder': {'driver_tbl': '2', 'driver_toff': '3', 'driver_hstrt': '5',
                             'driver_hend': '0', 'stealthchop_threshold': '0'},
        'extruder': {'min_extrude_temp': '170', 'rotation_distance': '3.5'},
    }
    driver, name, regs, stealth, min_temp = extruder_context(settings)
    assert name == '2209' and driver is tmc.DRIVERS['2209']
    assert regs == {'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 0}
    assert stealth is None and min_temp == 170


def test_extruder_context_requires_a_driver():
    with pytest.raises(SystemExit, match='extruder'):
        extruder_context({'extruder': {}})


def test_extruder_macro_args_translate():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    args = parser.parse_args(_gcode_args(
        ['extruder', 'TEMP=240', 'SPEED=5', 'SAVE=1', 'DRY_RUN=1'], boolean_flags(parser)))
    assert (args.temp == 240 and args.speed == 5 and args.save and args.dry_run
            and args.min_speed == 1 and args.max_speed == 12 and not args.save_last)
    args = parser.parse_args(_gcode_args(['extruder', 'SAVE_LAST=1'], boolean_flags(parser)))
    assert args.save_last


def test_winner_state_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(extruder_mod, 'STATE', str(tmp_path / 'extruder.json'))
    assert load_winner_state() is None
    save_winner_state('2209', tmc.Chopper(3, 7, 6, 0))
    state = load_winner_state()
    assert state == {'driver': '2209',
                     'fields': {'tbl': 3, 'toff': 7, 'hstrt': 6, 'hend': 0}}


def test_extruder_demo_arg_translates():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    # named DEMO (not SHOW): a boolean --show here would collide with belts' SHOW=A/B
    # through the cross-subcommand boolean_flags collection
    args = parser.parse_args(_gcode_args(['extruder', 'DEMO=1'], boolean_flags(parser)))
    assert args.demo and not args.save_last


def test_extruder_descent_rides_the_shared_engine(monkeypatch):
    # the E descent must be the same multi-start engine as the axis tune — a private
    # per-field walk re-created the measured toff x hend blind spot
    from chopper_autotune import extruder as ex
    from chopper_autotune import tmc
    captured = {}

    def fake_engine(driver, tbl, toff, hstrt, hend, tpfd, start, evaluate, rounds=2):
        captured.update(ranges=(tbl, toff, hstrt, hend), tpfd=tpfd, start=start, rounds=rounds)
        return tmc.Chopper(0, 2, 2, 12)

    monkeypatch.setattr(ex, 'multi_start_descent', fake_engine)
    winner, cache = ex.descent(None, None, tmc.DRIVERS['2209'], 5.0, 0.25, None)
    assert captured['ranges'] == ex.FIELD_RANGES
    assert captured['start'] == tmc.KLIPPER_DEFAULT
    assert captured['tpfd'] is None
    assert winner == tmc.Chopper(0, 2, 2, 12)


def test_extruder_descent_measures_live_and_caches(monkeypatch):
    from chopper_autotune import extruder as ex
    from chopper_autotune.collect import Range

    monkeypatch.setattr(ex, 'FIELD_RANGES',
                        (Range(2, 2), Range(3, 3), Range(4, 5), Range(0, 1)))
    measured = []

    def fake_measure(hw, speed):
        measured.append(speed)
        return 100.0, 0

    monkeypatch.setattr(ex, 'measure', fake_measure)

    class FakeKl:
        def gcode(self, script):
            pass

    class FakeScreen:
        def update(self, text, force=False):
            pass

    winner, cache = ex.descent(None, FakeKl(), ex.tmc.DRIVERS['2209'], 5.0, 0.25, FakeScreen())
    assert winner in cache
    assert len(measured) == len(cache)          # every combo measured exactly once (cache)
    assert (winner.tbl, winner.toff) == (2, 3)  # stayed inside the forced ranges


def test_extruder_context_reads_tpfd_on_tpfd_drivers():
    settings = {'tmc2240 extruder': {'driver_tbl': 2, 'driver_toff': 3, 'driver_hstrt': 5,
                                     'driver_hend': 2, 'driver_tpfd': 4},
                'extruder': {'min_extrude_temp': 170}}
    driver, name, regs, _, _ = extruder_context(settings)
    assert name == '2240' and regs['tpfd'] == 4
    # explicit stock lines read as stock, so the demo guard can refuse honestly
    from chopper_autotune import tmc
    assert tmc.baseline_chopper(regs, default=driver.default) == driver.default


def test_extruder_stealth_resolved_live_before_the_force():
    from chopper_autotune import tmc
    from chopper_autotune.extruder import resolve_extruder_stealth

    class LiveKl:
        def gcode_output(self, script):
            assert script == 'DUMP_TMC STEPPER=extruder REGISTER=GCONF'
            return ['// GCONF:      0000000e en_pwm_mode=1']
    driver = tmc.DRIVERS['2240']
    # no stealthchop_threshold line, yet en_pwm_mode=1 = stealthChop (autotune's doing)
    assert resolve_extruder_stealth(LiveKl(), driver, None) == ('en_pwm_mode', 0, 1)

    class DeafKl:
        def __init__(self, settings=None):
            self.config = settings or {}

        def gcode_output(self, script):
            return ['// ok']

        def settings(self):
            return self.config
    assert resolve_extruder_stealth(DeafKl(), driver, None) is None
    assert resolve_extruder_stealth(DeafKl(), driver, driver.spreadcycle_switch) == driver.spreadcycle_switch
    # unreadable: klipper_tmc_autotune's silent and autoswitch goals clear en_spreadcycle
    for goal, stealth in (('silent', driver.spreadcycle_switch), ('autoswitch', driver.spreadcycle_switch),
                          ('performance', None), ('auto', None)):
        deaf = DeafKl({'autotune_tmc extruder': {'tuning_goal': goal}})
        assert resolve_extruder_stealth(deaf, driver, None) == stealth, goal


def test_save_on_an_autotune_extruder_is_refused_before_the_heat_up(monkeypatch):
    from types import SimpleNamespace

    import pytest

    import chopper_autotune.extruder as extruder_mod
    scripts = []
    kl = SimpleNamespace(settings=lambda: {'tmc2209 extruder': {}, 'autotune_tmc extruder': {'motor': 'x'},
                                           'extruder': {}},
                         gcode=scripts.append)
    with pytest.raises(SystemExit, match=r'not saving \[tmc2209 extruder\]'):
        extruder_mod.extruder_tune(kl, SimpleNamespace(save_last=False, save=True, temp=200.0))
    assert scripts == []                                 # no M104: nothing heated
    # SAVE_LAST=1 persists nothing either
    state = {'driver': '2209', 'fields': {'tbl': 1, 'toff': 3, 'hstrt': 5, 'hend': 2}}
    monkeypatch.setattr(extruder_mod, 'load_winner_state', lambda: state)
    with pytest.raises(SystemExit, match=r'not saving \[tmc2209 extruder\]'):
        extruder_mod.extruder_tune(kl, SimpleNamespace(save_last=True, save=False, url='http://x'))
