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


def test_run_demo_report_reraises_a_sigterm_exit(monkeypatch):
    # CHOPPER_STOP lands as SystemExit(143) from the SIGTERM handler: it must stop
    # the whole demo, not be swallowed as "motor A skipped" with motor B still played
    played = []

    def stopped(kl, args):
        played.append(args.axis)
        raise SystemExit(143)

    monkeypatch.setattr(demo_module, 'demo', stopped)
    monkeypatch.setattr(demo_module, 'find_socket', lambda socket: 'sock')
    monkeypatch.setattr(demo_module, 'Klippy', fake_klippy)
    with pytest.raises(SystemExit):
        demo_module.run_demo(argparse.Namespace(axis='xy', report=True, socket=None))
    assert played == ['x']


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
    # CoreXY/H-Bot: stepper_x = X+Y, stepper_y = X-Y, so a diagonal gives the two
    # motors different speeds
    vx, vy = head_velocity('corexy', 58, 34)
    assert (vx, vy) == (46.0, 12.0)
    assert vx + vy == 58 and vx - vy == 34
    assert head_velocity('hbot', 58, 34) == (46.0, 12.0)
    # Cartesian: each motor drives its own axis directly
    assert head_velocity('cartesian', 58, 34) == (58.0, 34.0)
    # CoreXZ: the coupled pair is X/Z, untouched by an X/Y move -> identity too
    assert head_velocity('corexz', 58, 34) == (58.0, 34.0)


def test_show_requires_a_recorded_speed(monkeypatch):
    # a silent 50 mm/s fallback would play the show off resonance and prove nothing
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}))
    monkeypatch.setattr(demo_module, 'known_speed', lambda axis: None)
    with pytest.raises(SystemExit, match='no tuned speed'):
        demo_module.showcase_together(None, demo_args())


def test_show_writes_state_for_both_motors(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr('chopper_autotune.dataset.RESULTS_HOME', tmp_path)
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}))
    monkeypatch.setattr(demo_module, 'known_speed', lambda axis: 58 if axis == 'x' else 34)
    sweeps = iter([[2000.0], [1000.0]] * 2)
    monkeypatch.setattr(demo_module, '_sweep', lambda *a, **kw: next(sweeps))
    kl = type('K', (), {'gcode': lambda self, s: None,
                        'subscribe_accel': lambda self, chip: None,
                        'is_printing': lambda self: False})()

    assert demo_module.showcase_together(kl, demo_args(dry_run=False, rounds=2)) == 0
    state = json.loads((tmp_path / 'state.json').read_text())
    # the Show button must feed the panel's vibration column for both motors
    assert state == {'x': {'regs': '2/1/4/14', 'quieter': 2.0},
                     'y': {'regs': '2/1/4/14', 'quieter': 2.0}}


def test_scan_args_share_the_find_speed_parser_defaults():
    from chopper_autotune.cli import build_parser
    reference = build_parser().parse_args(['find-speed', '--axis', 'x', '--yes'])
    got = demo_module._scan_args(demo_args(csv=False, no_raw=True, dry_run=False))
    for field in ('min_speed', 'max_speed', 'step', 'iterations', 'measure_time'):
        assert getattr(got, field) == getattr(reference, field)


def test_sweep_spans_the_full_allowed_zone(monkeypatch):
    import math

    from chopper_autotune.collect import MOVE_MARGIN
    from chopper_autotune.demo import _sweep

    captured = []
    monkeypatch.setattr(demo_module, 'capture_stream',
                        lambda board, move, dur: (captured.append(move), (0.0, None))[1])
    monkeypatch.setattr(demo_module, 'vibration_score', lambda data, trim: {'median_magnitude': 100.0})
    board = type('B', (), {'kinematics': 'corexy', 'center': (130, 130),
                           'kl': type('K', (), {'gcode': lambda self, s: None})()})()
    span = 260
    _sweep(board, {'x': 58, 'y': 34}, 1000, span, demo_args(repeats=1))

    def xy(move):
        p = {seg[0]: float(seg[1:]) for seg in move.split() if seg[0] in 'XY'}
        return p['X'], p['Y']

    (bx, by), (ax, ay) = xy(captured[0]), xy(captured[1])       # the two diagonal endpoints
    # endpoint-to-endpoint distance is the stroke, sized to the whole allowed zone
    # (abs_tol covers the 2-decimal rounding of the gcode coordinates)
    assert math.isclose(math.hypot(bx - ax, by - ay), span * MOVE_MARGIN, abs_tol=0.05)


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
