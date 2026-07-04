import argparse

import pytest

import chopper_autotune.demo as demo_module
from chopper_autotune import tmc
from chopper_autotune.cli import _chopper
from chopper_autotune.collect import Hardware, Range
from chopper_autotune.demo import bar, demo


def test_bar_scales_and_has_min_width():
    assert bar(100, 100) == '#' * 40
    assert bar(50, 100) == '#' * 20
    assert bar(1, 100) == '#'          # never empty


def test_chopper_parser():
    assert _chopper('2,3,5,0') == tmc.Chopper(2, 3, 5, 0)
    assert _chopper('0/2/7/9') == tmc.Chopper(0, 2, 7, 9)
    with pytest.raises(argparse.ArgumentTypeError):
        _chopper('2,3,5')


def make_hw(baseline):
    return Hardware(kl=None, stepper='stepper_x', driver=tmc.DRIVERS['2209'],
                    accel_chip='adxl345', kinematics='corexy', axis_span=260,
                    center=(130, 130), max_accel=10000, baseline=baseline)


def demo_args(**over):
    base = dict(axis='x', speed=None, default=None, iterations=3, measure_time=1.0,
                accel=None, trim=None, socket=None, dry_run=True,
                report=False, rounds=3, repeats=4)
    base.update(over)
    return argparse.Namespace(**base)


def test_demo_rejects_when_not_yet_tuned(monkeypatch):
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 0}))
    with pytest.raises(SystemExit, match='nothing to demo'):
        demo(None, demo_args())


def test_demo_dry_run_reports_plan(monkeypatch, capsys):
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}))
    assert demo(None, demo_args(speed=Range(58, 58))) == 0
    out = capsys.readouterr().out
    assert 'Demo on motor A' in out
    assert 'defaults tbl2_toff3_hstrt5_hend0 vs tuned tbl2_toff1_hstrt4_hend14' in out


def test_demo_defaults_to_showcase_report_switches_to_numbers(monkeypatch, capsys):
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}))
    demo(None, demo_args(speed=Range(58, 58)))                     # report=False → audible show
    assert 'live showcase' in capsys.readouterr().out
    demo(None, demo_args(speed=Range(58, 58), report=True))        # REPORT=1 → measured numbers
    assert 'measure' in capsys.readouterr().out


def fake_klippy(*_):
    return type('K', (), {'connect': lambda self: self, 'close': lambda self: None})()


def test_run_demo_report_measures_both_motors(monkeypatch):
    played = []
    monkeypatch.setattr(demo_module, 'demo', lambda kl, args: played.append(args.axis) or 0)
    monkeypatch.setattr(demo_module, 'find_socket', lambda socket: 'sock')
    monkeypatch.setattr(demo_module, 'Klippy', fake_klippy)
    # MOTOR=AB REPORT=1 measures each motor in turn (the audible show goes through _together)
    assert demo_module.run_demo(argparse.Namespace(axis='xy', report=True, socket=None)) == 0
    assert played == ['x', 'y']


def test_run_demo_report_skips_an_untuned_motor(monkeypatch, capsys):
    def one_untuned(kl, args):
        if args.axis == 'x':
            raise SystemExit('nothing to demo')
        return 0

    monkeypatch.setattr(demo_module, 'demo', one_untuned)
    monkeypatch.setattr(demo_module, 'find_socket', lambda socket: 'sock')
    monkeypatch.setattr(demo_module, 'Klippy', fake_klippy)
    assert demo_module.run_demo(argparse.Namespace(axis='xy', report=True, socket=None)) == 2
    assert 'motor A skipped' in capsys.readouterr().out


def test_run_demo_together_is_the_default_for_both_motors(monkeypatch, capsys):
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}))
    monkeypatch.setattr(demo_module, 'known_speed', lambda axis: 58 if axis == 'x' else 34)
    monkeypatch.setattr(demo_module, 'find_socket', lambda socket: 'sock')
    monkeypatch.setattr(demo_module, 'Klippy', fake_klippy)
    code = demo_module.run_demo(argparse.Namespace(
        axis='xy', report=False, dry_run=True, socket=None, default=None, speed=None,
        accel=None, rounds=2, repeats=2, measure_time=1.0))
    out = capsys.readouterr().out
    assert code == 0
    assert 'both motors together' in out
    assert 'motor A at 58 and motor B at 34' in out          # each motor at its own resonance


def test_head_velocity_diagonal_puts_each_motor_at_its_speed():
    from chopper_autotune.demo import head_velocity
    # CoreXY: stepper_x = X+Y, stepper_y = X-Y, so a diagonal gives the two motors different speeds
    vx, vy = head_velocity('corexy', 58, 34)
    assert (vx, vy) == (46.0, 12.0)
    assert vx + vy == 58 and vx - vy == 34
    # Cartesian: each motor drives its own axis directly
    assert head_velocity('cartesian', 58, 34) == (58.0, 34.0)


def test_showcase_alternates_and_announces(monkeypatch, capsys):
    from chopper_autotune.collect import Screen
    from chopper_autotune.demo import _showcase

    played = []

    def fake_measurement(hw, ds, args, combo, speed, iteration, direction, travel, accel, before):
        played.append((combo.toff, direction))
        return {'status': 'ok', 'score': {'median_magnitude': 2000.0 if combo.toff == 3 else 900.0}}

    monkeypatch.setattr(demo_module, 'run_measurement', fake_measurement)
    kl = type('K', (), {'gcode': lambda self, s: None})()
    hw = make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14})
    configs = [('default', tmc.Chopper(2, 3, 5, 0)), ('tuned', tmc.Chopper(2, 1, 4, 14))]
    args = demo_args(rounds=2, repeats=1)

    results = _showcase(kl, hw, args, None, configs, 58, 70.0, 1000,
                        lambda d, t: None, Screen(kl, display=False))

    # 2 rounds x 2 configs x 1 repeat x 2 directions
    assert len(played) == 8
    assert statistics_mean(results['default']) == 2000.0
    assert statistics_mean(results['tuned']) == 900.0
    out = capsys.readouterr().out
    assert 'DEFAULTS' in out and 'TUNED' in out and 'round 1/2' in out
    assert '2.2x less vibration' in out          # per-round comparison announced


def statistics_mean(xs):
    return sum(xs) / len(xs)


def test_write_state_merges_per_motor(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr('chopper_autotune.dataset.RESULTS_HOME', tmp_path)
    demo_module.write_state('x', tmc.Chopper(2, 1, 4, 14), 2.375)
    assert json.loads((tmp_path / 'state.json').read_text()) == {'x': {'regs': '2/1/4/14', 'quieter': 2.38}}
    demo_module.write_state('y', tmc.Chopper(0, 2, 7, 9), 1.9)          # merged, not overwritten
    state = json.loads((tmp_path / 'state.json').read_text())
    assert set(state) == {'x', 'y'} and state['y'] == {'regs': '0/2/7/9', 'quieter': 1.9}


def test_known_speed_reuses_latest_axis_run(tmp_path, monkeypatch):
    from chopper_autotune.dataset import Dataset
    from chopper_autotune.demo import known_speed

    Dataset.create(tmp_path / '01_x', {'axis': 'x', 'search': 'grid', 'speeds': [58]})
    Dataset.create(tmp_path / '02_y', {'axis': 'y', 'search': 'descent', 'speeds': [34]})
    Dataset.create(tmp_path / '03_x', {'axis': 'x', 'mode': 'find-speed',
                                       'speeds': [20, 22, 24]})     # ignored
    Dataset.create(tmp_path / '04_x', {'axis': 'x', 'mode': 'demo', 'speed': 20})  # ignored
    monkeypatch.setattr('chopper_autotune.analyze.dataset_dirs',
                        lambda: sorted((tmp_path).iterdir()))

    assert known_speed('x') == 58          # the tuning run, not the later demo/scan
    assert known_speed('y') == 34
    assert known_speed('z') is None
