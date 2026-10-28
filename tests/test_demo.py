import argparse
import re
import types

import pytest

import chopper_autotune.demo as demo_module
import klipper_front
from chopper_autotune import collect, extruder, tmc
from chopper_autotune.cli import _chopper, build_parser
from chopper_autotune.collect import Hardware
from chopper_autotune.demo import bar, demo
from test_apply_demo import run_demo, tuned
from test_klipper_front import HOTEND, SOURCES, assert_clean, printer_cfg, require

# objects/query of the live limits (live_limits)
LIVE = {'status': {'toolhead': {'max_velocity': 500.0, 'max_accel': 3000.0,
                                'minimum_cruise_ratio': 0.5}, 'gcode_move': {'speed_factor': 1.0}}}


def test_bar_scales_and_has_min_width():
    assert bar(100, 100) == '#' * 40
    assert bar(50, 100) == '#' * 20
    assert bar(1, 100) == '#'          # never empty


def test_chopper_parser():
    assert _chopper('2,3,5,0') == tmc.Chopper(2, 3, 5, 0)
    assert _chopper('0/2/7/9') == tmc.Chopper(0, 2, 7, 9)
    with pytest.raises(argparse.ArgumentTypeError):
        _chopper('2,3,5')


def make_hw(baseline, stepper='stepper_x'):
    # rail_of asks the settings for twins: none here
    return Hardware(kl=types.SimpleNamespace(settings=lambda: {}), stepper=stepper,
                    driver=tmc.DRIVERS['2209'], accel_chip='adxl345', kinematics='corexy',
                    axis_span=260, center=(130, 130), max_accel=10000, baseline=baseline)


def demo_args(**over):
    """CHOPPER_DEMO MOTOR=A as the parser gives it, a dry run, with `over` on top."""
    args = build_parser().parse_args(['demo', '--motor', 'a', '--dry-run'])
    vars(args).update(over)
    return args


def test_demo_rejects_when_not_yet_tuned(monkeypatch):
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 0}))
    with pytest.raises(SystemExit, match='^tune and save motor A first: the config that Klipper '
                                         'loaded gives the Klipper defaults '):
        demo(None, demo_args())


def test_demo_dry_run_reports_plan(monkeypatch, capsys):
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}))
    assert demo(None, demo_args(speed=58)) == 0
    out = capsys.readouterr().out
    assert 'Demo on motor A' in out
    assert 'defaults tbl2_toff3_hstrt5_hend0 vs tuned tbl2_toff1_hstrt4_hend14' in out


def test_demo_defaults_to_showcase_report_switches_to_numbers(monkeypatch, capsys):
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}))
    demo(None, demo_args(speed=58))                     # report=False → audible show
    assert 'live showcase' in capsys.readouterr().out
    demo(None, demo_args(speed=58, report=True))        # REPORT=1 → measured numbers
    assert 'measure' in capsys.readouterr().out


def fake_klippy(*_):
    return type('K', (), {'connect': lambda self: self, 'close': lambda self: None,
                          'settings': lambda self: {}})()


def test_run_demo_report_measures_both_motors(monkeypatch):
    played = []
    monkeypatch.setattr(demo_module, 'demo', lambda kl, args: played.append(args.axis) or 0)
    monkeypatch.setattr(demo_module, 'find_socket', lambda socket: 'sock')
    monkeypatch.setattr(demo_module, 'Klippy', fake_klippy)
    # MOTOR=AB REPORT=1 measures each motor in turn (the audible show goes through _together).
    # Here and below a dry run: no driver read before the motors, which demo() fakes
    assert demo_module.run_demo(argparse.Namespace(axis='xy', report=True, socket=None, speed=None,
                                                   dry_run=True)) == 0
    assert played == ['x', 'y']


def test_run_demo_report_skips_an_untuned_motor(monkeypatch, capsys):
    def one_untuned(kl, args):
        if args.axis == 'x':
            raise SystemExit('nothing to demo')
        return 0

    monkeypatch.setattr(demo_module, 'demo', one_untuned)
    monkeypatch.setattr(demo_module, 'find_socket', lambda socket: 'sock')
    monkeypatch.setattr(demo_module, 'Klippy', fake_klippy)
    assert demo_module.run_demo(argparse.Namespace(axis='xy', report=True, socket=None, speed=None,
                                                   dry_run=True)) == 2
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
        demo_module.run_demo(argparse.Namespace(axis='xy', report=True, socket=None, speed=None,
                                                dry_run=True))
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


TUNED = {'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}


@pytest.mark.parametrize('argv, b, advice', [
    # motor B has tuned registers: alone, the demo scans for its speed and plays them
    (['demo', '--rounds', '2', '--default', '2,3,5,1', '--dry-run'], TUNED,
     'no tuned speed recorded for motor B. The command that plays only motor B scans for its '
     'resonance speed first: CHOPPER_DEMO MOTOR=B DEFAULT=2,3,5,1 ROUNDS=2 DRY_RUN=1'),
    # motor B runs the defaults: on its own it has nothing to play against them
    (['demo', '--rounds', '2'], {'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 0},
     'no tuned speed recorded for motor B. Its registers equal the defaults, so it has nothing to '
     'show. To tune it: CHOPPER_TUNE MOTOR=B')])
def test_show_requires_a_recorded_speed(monkeypatch, argv, b, advice):
    # a silent 50 mm/s fallback would play the show off resonance and prove nothing
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw(TUNED if axis == 'x' else b, 'stepper_' + axis))
    monkeypatch.setattr(demo_module, 'known_speed', lambda axis: None if axis == 'y' else 58)
    parser = build_parser()
    with pytest.raises(SystemExit) as refused:
        demo_module.showcase_together(None, parser.parse_args(argv))
    assert refused.value.code == advice
    # pasted, the command runs what the show could not: SPEED= would play one motor
    words = advice.split(': ')[-1].split()
    pasted = parser.parse_args(_gcode_args([words[0][len('CHOPPER_'):].lower()] + words[1:],
                                           boolean_flags(parser)))
    if pasted.command == 'demo':
        assert vars(pasted) == dict(vars(parser.parse_args(argv)), axis='y')
        assert demo(None, pasted) == 0
    else:
        assert pasted.command == 'tune' and pasted.axis == 'y'
        with pytest.raises(SystemExit, match='^tune and save motor B first'):
            demo(None, parser.parse_args(['demo', '--motor', 'b', '--dry-run']))


def tuned_motors(monkeypatch):
    """A printer whose config gives both motors tuned registers."""
    monkeypatch.setattr(demo_module, 'find_socket', lambda socket: 'sock')
    monkeypatch.setattr(demo_module, 'Klippy', fake_klippy)
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis, accel=True: make_hw(TUNED, 'stepper_' + axis))


@pytest.mark.parametrize('argv, one', [
    (['demo', '--speed', '40', '--dry-run'], 'CHOPPER_DEMO MOTOR=A SPEED=40 DRY_RUN=1'),
    (['demo', '--motor', 'ab', '--speed', '40', '--report', '--dry-run'],
     'CHOPPER_DEMO MOTOR=A SPEED=40 REPORT=1 DRY_RUN=1'),
    (['demo', '--speed', '40', '--default', '2/3/5/1', '--dry-run'],
     'CHOPPER_DEMO MOTOR=A SPEED=40 DEFAULT=2,3,5,1 DRY_RUN=1')])
def test_a_demo_of_both_motors_takes_no_speed(monkeypatch, argv, one):
    # one SPEED on both motors plays at least one of them off its resonance
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    from chopper_autotune.collect import failure_display
    tuned_motors(monkeypatch)
    with pytest.raises(SystemExit) as refused:
        demo_module.run_demo(build_parser().parse_args(argv))
    assert refused.value.code == ('add MOTOR=A or B, or leave SPEED out: each motor has its own '
                                  'resonance speed. Without SPEED, each motor plays at the speed of '
                                  'its last tune. To play only motor A at this speed: ' + one)
    assert failure_display('demo FAILED: ' + refused.value.code)[:16] == 'FAIL add MOTOR=A'
    # pasted, it plays motor A at that speed
    parser = build_parser()
    pasted = parser.parse_args(_gcode_args(['demo'] + one.split()[1:], boolean_flags(parser)))
    assert vars(pasted) == dict(vars(build_parser().parse_args(argv)), axis='x')


@pytest.mark.parametrize('recorded, motor', [
    ({}, 'A'), ({'x': 58}, 'B'), ({'y': 34}, 'A'), ({'x': 58, 'y': 34}, 'A')])
def test_a_demo_of_both_motors_with_a_speed_names_the_motor_that_has_none(monkeypatch, recorded,
                                                                         motor):
    # without a speed from its last tune, the show of both refuses that motor and the report
    # scans it: SPEED= is for it
    from chopper_autotune.cli import build_parser
    tuned_motors(monkeypatch)
    monkeypatch.setattr(demo_module, 'known_speed', recorded.get)
    with pytest.raises(SystemExit) as refused:
        demo_module.run_demo(build_parser().parse_args(['demo', '--speed', '40', '--report',
                                                        '--dry-run']))
    assert refused.value.code.endswith(' To play only motor %s at this speed: CHOPPER_DEMO MOTOR=%s '
                                       'SPEED=40 REPORT=1 DRY_RUN=1' % (motor, motor))


def show_of_both(monkeypatch, tmp_path, sweeps: list, b: dict = TUNED):
    """A printer with a display for showcase_together, whose motor A runs TUNED and motor B
    `b`, and whose sweeps measure `sweeps` in turn, in the order the show plays the sets
    (DEFAULTS first in round 1, TUNED in round 2); and the scripts it is sent."""
    from dataclasses import replace
    monkeypatch.setattr('chopper_autotune.dataset.RESULTS_HOME', tmp_path)
    monkeypatch.setattr(demo_module, 'detect_hardware', lambda kl, axis: replace(
        make_hw(TUNED if axis == 'x' else b, 'stepper_' + axis), display=True))
    monkeypatch.setattr(demo_module, 'known_speed', lambda axis: 58 if axis == 'x' else 34)
    sweeps = iter(sweeps)
    monkeypatch.setattr(demo_module, '_sweep', lambda *a, **kw: next(sweeps))
    sent = []
    kl = type('K', (), {'gcode': lambda self, s: sent.append(s),
                        'request': lambda self, *args: LIVE,
                        'gcode_output': lambda self, s: [],
                        'subscribe_accel': lambda self, chip: None,
                        'settings': lambda self: {},          # no driver sections: guard idle
                        'is_printing': lambda self: False})()
    return kl, sent


def test_each_round_of_the_show_prints_its_own_vibrations_and_gain_with_two_decimals(
        tmp_path, monkeypatch, capsys):
    # with one decimal, a gain under 5% reads 1.0x, as if tuning gave nothing; in round 2
    # and overall TUNED vibrates more, which "less vibration" would hide
    from chopper_autotune.collect import LCD_WIDTH
    kl, sent = show_of_both(monkeypatch, tmp_path, [[1040.0], [1000.0], [1100.0], [1000.0]])
    assert demo_module.showcase_together(kl, demo_args(dry_run=False, rounds=2)) == 0
    out = capsys.readouterr().out
    rounds = ['both motors, defaults 1040 -> tuned 1000: 1.04x less vibration',
              'both motors, defaults 1000 -> tuned 1100: 1.10x more vibration']
    assert [line[len('   => '):] for line in out.splitlines() if line.startswith('   => ')] \
        == rounds
    assert [script for script in sent if script.startswith('RESPOND ') and ' -> ' in script] \
        == ['RESPOND PREFIX="Chopper:" MSG="%s"' % line for line in rounds]
    assert '\nboth motors together: 1.03x more vibration overall\n' in out     # 1050 / 1020
    assert 'M118 Chopper: both motors together 1.03x more vibration' in sent
    shown = [script[len('M117 '):] for script in sent if script.startswith('M117 ')]
    assert [text for text in shown if 'vib' in text] == [
        '1.04x less vib', '1.10x more vib', '1.03x more vib']   # each round, then the result
    assert max(len(text) for text in shown) <= LCD_WIDTH


def test_the_show_leaves_the_panel_figure_of_each_motor_as_it_was(tmp_path, monkeypatch):
    # the panel shows a figure per motor: the show's would credit each motor with the gain
    # of both together, a motor that gains nothing too
    kl, _ = show_of_both(monkeypatch, tmp_path, [[2000.0], [1000.0]])
    demo_module.write_state('x', tmc.Chopper(**TUNED), 1.3)        # motor A's own run
    before = (tmp_path / 'state.json').read_text()
    assert demo_module.showcase_together(kl, demo_args(dry_run=False, rounds=1)) == 0
    assert (tmp_path / 'state.json').read_text() == before


@pytest.mark.parametrize('b, played', [(TUNED, 'AB'),
                                      ({'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 0}, 'A')],
                         ids=['both tuned', 'B on the defaults'])
def test_the_show_ends_with_the_demo_of_each_motor(tmp_path, monkeypatch, capsys, b, played):
    # the show measures both motors together; the demo of only a motor on the defaults has
    # nothing to play
    from chopper_autotune.cli import build_parser
    from test_next_step import pasted
    kl, sent = show_of_both(monkeypatch, tmp_path, [[2000.0], [1000.0]], b)
    argv = ['demo', '--rounds', '1', '--repeats', '2']
    assert demo_module.showcase_together(kl, build_parser().parse_args(argv)) == 0
    lines = ['To see the gain of motor %s, play only that motor: CHOPPER_DEMO MOTOR=%s ROUNDS=1 '
             'REPEATS=2' % (motor, motor) for motor in played]
    assert capsys.readouterr().out.endswith(' less vibration overall%s\n\n%s\n' % (
        '' if played == 'AB' else '; motor B played the same registers in DEFAULTS and TUNED',
        '\n'.join(lines)))
    final = next(index for index, script in enumerate(sent) if script.startswith('M118 '))
    assert sent[final + 1:] == ['RESPOND PREFIX="Chopper:" MSG="%s"' % line for line in lines]
    for line, axis in zip(lines, 'xy'):
        assert vars(pasted(line)) == dict(vars(build_parser().parse_args(argv)), axis=axis)


def test_show_refuses_a_hot_driver_and_writes_nothing_after_the_release(tmp_path, monkeypatch):
    # the preflight runs outside the try: after its release the finally would write toff
    # into the switched-off drivers and re-energize a hot one (no enable pin, #133)
    import chopper_autotune.collect as collect_mod
    from chopper_autotune.collect import DriverTooHot
    monkeypatch.setattr(collect_mod, 'PREFLIGHT_SEC', 0)
    monkeypatch.setattr('chopper_autotune.dataset.RESULTS_HOME', tmp_path)
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}))
    monkeypatch.setattr(demo_module, 'known_speed', lambda axis: 58 if axis == 'x' else 34)
    scripts = []
    kl = type('K', (), {'gcode': lambda self, s: scripts.append(s),
                        'gcode_output': lambda self, s: [],
                        'subscribe_accel': lambda self, chip: None,
                        'settings': lambda self: {'stepper_x': {}, 'stepper_y': {},
                                                  'tmc2209 stepper_x': {}, 'tmc2209 stepper_y': {}},
                        'subscribe_status': lambda self, objects: None,
                        'status': lambda self: {'tmc2209 stepper_x': {'drv_status': {'otpw': 1}}},
                        'is_printing': lambda self: False,
                        'homed_axes': lambda self: 'xyz', 'info': lambda self: {},
                        'stepper_states': lambda self: {'stepper_x': True, 'stepper_y': True,
                                                        'stepper_z': True}})()
    with pytest.raises(DriverTooHot):
        demo_module.showcase_together(kl, demo_args(dry_run=False, rounds=1))
    assert 'SET_STEPPER_ENABLE STEPPER="stepper_x" ENABLE=0' in scripts[-1] and 'M18' not in scripts[-1]
    assert not any('SET_TMC_FIELD' in s or 'G28' in s for s in scripts)


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

    # round 1: defaults 1040, tuned 1000; round 2: defaults 1000, tuned 1020
    vibration = {(1, 3): 1040.0, (1, 1): 1000.0, (2, 3): 1000.0, (2, 1): 1020.0}

    def fake_measurement(hw, ds, args, combo, speed, iteration, direction, travel, accel, before):
        played.append((combo.toff, direction))
        return {'status': 'ok',
                'score': {'median_magnitude': vibration[(len(played) + 3) // 4, combo.toff]}}

    monkeypatch.setattr(demo_module, 'run_measurement', fake_measurement)
    sent = []
    kl = type('K', (), {'gcode': lambda self, s: sent.append(s)})()
    hw = make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14})
    configs = [('default', tmc.Chopper(2, 3, 5, 0)), ('tuned', tmc.Chopper(2, 1, 4, 14))]
    args = demo_args(rounds=2, repeats=1)

    results = _showcase(kl, hw, args, None, configs, 58, 70.0, 1000,
                        lambda d, t: None, Screen(kl, display=True))

    # 2 rounds x 2 configs x 1 repeat x 2 directions
    assert len(played) == 8
    assert statistics_mean(results['default']) == 1020.0
    assert statistics_mean(results['tuned']) == 1010.0
    out = capsys.readouterr().out
    assert 'DEFAULTS' in out and 'TUNED' in out and 'round 1/2' in out
    # each round its own vibrations, a gain under 5% with two decimals, not 1.0x, and
    # "more" where TUNED vibrates more
    assert [line for line in out.splitlines() if line.startswith('   => ')] == [
        '   => motor A, defaults 1040 -> tuned 1000: 1.04x less vibration',
        '   => motor A, defaults 1000 -> tuned 1020: 1.02x more vibration']
    assert [script for script in sent if script.startswith('M117 A ')] == [
        'M117 A 1.04x less vib', 'M117 A 1.02x more vib']


def statistics_mean(xs):
    return sum(xs) / len(xs)


# the sets the shows below play: Klipper defaults, and a tuned set of its own on motor A,
# motor B and the extruder
STOCK = tmc.DRIVERS['2209'].default
PLAYED = {'stepper_x': tmc.Chopper(2, 4, 5, 3), 'stepper_y': tmc.Chopper(2, 5, 5, 2),
          'extruder': tmc.Chopper(1, 4, 5, 2)}
SHOWS = {
    # (the command line, its parameter of the number of rounds, the drivers it plays on)
    'one motor': (['demo', '--motor', 'a', '--speed', '60'], '--rounds', ('stepper_x',)),
    # --speed: the speed of each motor's last tune (run_demo)
    'both motors': (['demo', '--speed', '60'], '--rounds', ('stepper_x', 'stepper_y')),
    'report': (['demo', '--motor', 'a', '--speed', '60', '--report'], '--iterations',
               ('stepper_x',)),
    # 2 rounds, always
    'extruder': (['extruder', '--demo', '--speed', '5', '--yes'], None, ('extruder',)),
}
CHOPPER_FIELDS = ('tbl', 'toff', 'hstrt', 'hend')


def four_rounds(show: str) -> 'list[str]':
    """The arguments of 4 rounds, one back-and-forth of each set in a round; the extruder
    plays its own 2 rounds."""
    return {'--rounds': ['--rounds', '4', '--repeats', '1'],
            '--iterations': ['--iterations', '4']}.get(SHOWS[show][1], [])


def run_show(source, monkeypatch, show: str, argv: 'list[str]' = (), cfg: 'str | None' = None):
    """The show on a printer built from a release's own modules (tests/klipper_front.py)
    whose config gives each driver its PLAYED set; argv after the show's command line."""
    require(source)
    monkeypatch.setattr(collect, 'time', klipper_front.Clock())
    front = klipper_front.Front(source, cfg or tuned(printer_cfg(extra=HOTEND), **PLAYED))
    argv = SHOWS[show][0] + list(argv)
    if argv[0] == 'extruder':
        assert extruder.extruder_tune(front.connect(), build_parser().parse_args(argv)) == 0
    else:
        assert run_demo(front, monkeypatch, argv[1:]) == 0
    assert_clean(front)
    return front


def turns(front, steppers: 'tuple[str, ...]') -> 'list[dict]':
    """The turns of a show up to its last move, in the order they played: the set written
    into the drivers of `steppers` ('D' Klipper defaults, 'T' the PLAYED set), the set the
    display and the console named after it and before the turn's first move, and the moves
    the turn made."""
    moving = [index for index, script in enumerate(front.scripts)
              if re.search(r'^(FORCE_MOVE|G1) ', script, re.M)]
    played = []
    for script in front.scripts[:moving[-1] + 1]:
        written = {}
        for stepper, field, value in re.findall(r'^SET_TMC_FIELD STEPPER=(\S+) FIELD=(\w+) '
                                                r'VALUE=(\d+)$', script, re.M):
            written.setdefault(stepper, {})[field] = int(value)
        for stepper in steppers:
            fields = written.get(stepper, {})
            if not set(CHOPPER_FIELDS) <= set(fields):
                continue
            regs = tmc.Chopper(*(fields[name] for name in CHOPPER_FIELDS))
            name = {STOCK: 'D', PLAYED[stepper]: 'T'}[regs]
            if stepper == steppers[0]:
                played.append({'set': name, 'display': None, 'console': None, 'moves': 0})
            else:
                assert played[-1]['set'] == name        # every motor in the same turn
        if not played:
            continue
        shown = re.fullmatch(r'M117 (?:E )?\d/\d (DEFAULT|TUNED)S?', script)
        told = re.fullmatch(r'RESPOND PREFIX="Chopper:" MSG="(?:E )?\d/\d:? +(?:>> )?'
                            r'(DEFAULTS|TUNED)"', script)
        for channel, said in (('display', shown), ('console', told)):
            if said and not played[-1]['moves']:
                played[-1][channel] = said.group(1)[0]
        played[-1]['moves'] += len(re.findall(r'^(?:FORCE_MOVE|G1) ', script, re.M))
    return played


def listened(out: str) -> 'list[str]':
    """The sets ('D' or 'T') as the show's console lines name them when each starts."""
    return [name[0].upper() for name in re.findall(
        r'^(?:>> round \d/\d  >> | round \d: )(DEFAULTS|TUNED|defaults|tuned)\b', out, re.M)]


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('show', SHOWS)
def test_each_show_writes_the_two_sets_in_turns_tuned_first_every_other_round(source, show,
                                                                             monkeypatch):
    # with the defaults always first, a vibration that rises over the show always lands
    # more on the tuned set
    front = run_show(source, monkeypatch, show, four_rounds(show))
    assert ''.join(turn['set'] for turn in turns(front, SHOWS[show][2])) == \
        ('DTTD' if show == 'extruder' else 'DTTDDTTD')


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('show', [show for show in SHOWS if show != 'report'])
def test_the_display_and_the_console_name_each_set_as_it_starts_to_play(source, show,
                                                                       monkeypatch, capsys):
    # the listener hears the sets in turns: each is named on its turn, before its moves
    played = turns(run_show(source, monkeypatch, show, four_rounds(show)), SHOWS[show][2])
    assert len(played) == (4 if show == 'extruder' else 8)
    for turn in played:
        assert turn['moves'] and turn['display'] == turn['console'] == turn['set'], turn
    assert listened(capsys.readouterr().out) == [turn['set'] for turn in played]


@pytest.mark.parametrize('show, rounds', [(show, rounds) for show in SHOWS
                                          for rounds in ((None, '2', '6') if SHOWS[show][1]
                                                         else (None,))],
                         ids=lambda value: value or 'default')
def test_a_steady_drift_over_the_show_leaves_two_like_sets_at_1_00x(show, rounds, monkeypatch):
    # both sets shake the head alike, more each second: with each set first in half the
    # rounds, the drift hits both alike
    def level(front, move):
        return 1000. + 10. * move['window'][0]         # mg, 10 mg more each second
    monkeypatch.setattr(klipper_front.Front, 'vibration', level)
    monkeypatch.setattr(klipper_front.Front, 'head_vibration', level)
    gains = []
    write_state, verdict = demo_module.write_state, demo_module._verdict

    def own_gain(axis, tuned, quieter):
        gains.append(quieter)
        write_state(axis, tuned, quieter)

    def gain_of_both(kl, args, results, *rest):
        gains.append(statistics_mean(results['default']) / statistics_mean(results['tuned']))
        return verdict(kl, args, results, *rest)
    monkeypatch.setattr(demo_module, 'write_state', own_gain)
    monkeypatch.setattr(demo_module, '_verdict', gain_of_both)
    front = run_show(SOURCES[-1], monkeypatch, show,
                     [SHOWS[show][1], rounds] if rounds else [])
    moves = front.moves or front.head_moves
    assert level(front, moves[-1]) > 1.05 * level(front, moves[0])    # a drift that shows
    assert gains == [pytest.approx(1.0, abs=0.001)]


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('show', ['one motor', 'report'])
def test_the_noise_floor_of_one_motor_runs_with_every_xy_driver_off_so_no_mode_reaches_it(
        source, show, monkeypatch, capsys):
    # the config asks for stealthChop, the moves run in spreadCycle: a driver that is off
    # runs in neither, so the floor the gain is counted above is the same in both
    cfg = tuned(printer_cfg(extra=HOTEND), **PLAYED).replace(
        '[tmc2209 stepper_x]\n', '[tmc2209 stepper_x]\nstealthchop_threshold: 999999\n')
    on, capture = [], collect.capture_stream

    def captured(hw, script, duration, check=None):
        if re.fullmatch(r'G4 P\d+', script):                  # the noise floor's dwell
            on.append(sorted(name for name, enabled in hw.kl.stepper_states().items()
                             if enabled and name != 'stepper_z'))
        return capture(hw, script, duration, check)
    monkeypatch.setattr(collect, 'capture_stream', captured)
    argv = ['--iterations', '1'] if show == 'report' else ['--rounds', '1', '--repeats', '1']
    front = run_show(source, monkeypatch, show, argv, cfg)
    assert on == [[]]
    field, spread, _ = tmc.DRIVERS['2209'].spreadcycle_switch
    assert front.moves and all(front.chopper(move, 'stepper_x')[field] == spread
                               for move in front.moves)
    assert front.chips['tmc2209 stepper_x'].field(field) != spread      # stealthChop back
    assert re.search(r'\n  \d\.\d\dx (less|more) vibration above the \d+ noise floor\n',
                     capsys.readouterr().out)


def test_write_state_merges_per_motor(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr('chopper_autotune.dataset.RESULTS_HOME', tmp_path)
    demo_module.write_state('x', tmc.Chopper(2, 1, 4, 14), 2.375)
    assert json.loads((tmp_path / 'state.json').read_text()) == {
        'x': {'regs': '2/1/4/14', 'quieter': 2.38, 'own': True}}
    demo_module.write_state('y', tmc.Chopper(0, 2, 7, 9), 1.9)          # merged, not overwritten
    state = json.loads((tmp_path / 'state.json').read_text())
    assert set(state) == {'x', 'y'} and state['y'] == {'regs': '0/2/7/9', 'quieter': 1.9,
                                                      'own': True}


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


def test_demo_shrinks_the_cruise_instead_of_skipping_a_fast_motor():
    # the re-tensioned rig moved a resonance to ~100 mm/s: the default 1.0s cruise
    # no longer fits the axis, and the demo used to refuse the motor outright
    from chopper_autotune.collect import MOVE_MARGIN, fit_measure_time, travel_for
    span, accel = 260.0, 1000.0
    fitted = fit_measure_time([100], accel, span * MOVE_MARGIN, 1.0)
    assert 0.4 <= fitted < 1.0                       # shrunk, not refused
    assert travel_for(100, accel, fitted) <= span * MOVE_MARGIN


def test_write_state_keeps_the_tpfd_register(tmp_path, monkeypatch):
    import json

    from chopper_autotune import tmc
    monkeypatch.setattr('chopper_autotune.dataset.RESULTS_HOME', tmp_path)
    demo_module.write_state('x', tmc.Chopper(2, 3, 5, 2, 4), 1.5)
    demo_module.write_state('y', tmc.Chopper(0, 2, 7, 11), 1.2)
    state = json.loads((tmp_path / 'state.json').read_text())
    # the panel matches these against the config's registers, tpfd included on 2240/5160
    assert state['x']['regs'] == '2/3/5/2/4' and state['y']['regs'] == '0/2/7/11'


def test_the_show_puts_back_autotunes_registers_read_live(monkeypatch):
    # autotune on X: its driver_* lines (none here) are not what the motor runs; the show
    # plays and puts back the registers read live, the other motor its config ones
    import argparse

    import chopper_autotune.demo as demo_module
    from chopper_autotune import tmc
    from chopper_autotune.collect import Hardware
    chopconf = {'stepper_x': '// CHOPCONF:   14410153 toff=4 hstrt=3 hend=6 tbl=1',
                'stepper_y': '// CHOPCONF:   14410153 toff=3 hstrt=4 hend=14 tbl=2'}

    class Kl:
        def __init__(self):
            self.sent = []

        def settings(self):
            return {'autotune_tmc stepper_x': {'tuning_goal': 'performance'}}

        def gcode(self, script):
            self.sent.append(script)

        def gcode_output(self, script):
            self.sent.append(script)
            if 'CHOPCONF' in script:
                return [chopconf[script.split('STEPPER=')[1].split()[0]]]
            return ['// GCONF: 00000004 en_spreadcycle=1']

        def subscribe_accel(self, chip):
            pass

        def request(self, method, params=None):
            return LIVE

    kl = Kl()

    def hardware(kl_, axis):
        return Hardware(kl=kl, stepper='stepper_' + axis, driver=tmc.DRIVERS['2209'],
                        accel_chip='adxl345', kinematics='corexy', axis_span=260,
                        center=(130, 130), max_accel=10000,
                        baseline={} if axis == 'x' else {'tbl': 2, 'toff': 3, 'hstrt': 4, 'hend': 14},
                        autotune='performance' if axis == 'x' else None)
    monkeypatch.setattr(demo_module, 'detect_hardware', hardware)
    monkeypatch.setattr(demo_module, 'known_speed', lambda axis: 58)
    monkeypatch.setattr(demo_module, 'home_xy', lambda kl, script: None)
    monkeypatch.setattr(demo_module, 'rehome_unless_hot', lambda kl: kl.gcode('G28 X Y'))
    monkeypatch.setattr(demo_module, 'refuse_blind_z_hop', lambda kl, settings: None)
    monkeypatch.setattr(demo_module, 'refuse_if_printing', lambda kl: None)
    monkeypatch.setattr(demo_module, 'ThermalGuard', lambda kl, settings: type(
        'G', (), {'preflight': lambda self: None, 'check': lambda self: None})())
    monkeypatch.setattr(demo_module, '_sweep', lambda *args, **kwargs: [1.0])
    monkeypatch.setattr(demo_module, 'write_state', lambda *args: None)
    demo_module.showcase_together(kl, argparse.Namespace(default=None, speed=None, accel=None,
                                                         rounds=1, repeats=1, dry_run=False))
    writes = [s for s in kl.sent if s.startswith('SET_TMC_FIELD STEPPER=stepper_x FIELD=t')
              or s.startswith('SET_TMC_FIELD STEPPER=stepper_x FIELD=h')]
    last_x = {line.split('FIELD=')[1].split()[0]: int(line.split('VALUE=')[1])
              for script in writes[-1:] for line in script.split('\n')}
    assert last_x == {'tbl': 1, 'toff': 4, 'hstrt': 3, 'hend': 6}
    assert kl.sent.index('G28 X Y') > kl.sent.index(writes[-1])      # restored, then re-homed


def test_a_print_stops_the_whole_report(monkeypatch):
    # a running or paused print refuses every motor before any: one refusal, announced, no
    # 'skipped'
    from argparse import Namespace

    from chopper_autotune.collect import PrinterBusy, refuse_if_printing
    tried = []

    def demo(kl, args):
        tried.append(args.axis)
        refuse_if_printing(kl)
    monkeypatch.setattr(demo_module, 'demo', demo)
    monkeypatch.setattr(demo_module, 'Klippy', lambda path: type('K', (), {
        'connect': lambda self: self, 'close': lambda self: None, 'is_printing': lambda self: True,
        'settings': lambda self: {'stepper_x': {}, 'stepper_y': {}}})())
    monkeypatch.setattr(demo_module, 'find_socket', lambda explicit=None: '<sock>')
    with pytest.raises(PrinterBusy, match='busy printing'):
        demo_module.run_demo(Namespace(axis='xy', report=True, socket=None, speed=None,
                                       dry_run=False))
    assert tried == []


def test_an_unsupported_klipper_stops_the_whole_report(monkeypatch):
    # detect_hardware refuses inside the per-motor loop: not 'motor A/B skipped', a stop
    # the display shows
    from argparse import Namespace

    from chopper_autotune.collect import UnsupportedKlipper
    tried = []

    def demo(kl, args):
        tried.append(args.axis)
        raise UnsupportedKlipper('this Klipper is too old')
    monkeypatch.setattr(demo_module, 'demo', demo)
    monkeypatch.setattr(demo_module, 'Klippy', lambda path: type('K', (), {
        'connect': lambda self: self, 'close': lambda self: None,
        'settings': lambda self: {'stepper_x': {}, 'stepper_y': {}}})())
    monkeypatch.setattr(demo_module, 'find_socket', lambda explicit=None: '<sock>')
    with pytest.raises(UnsupportedKlipper):
        demo_module.run_demo(Namespace(axis='xy', report=True, socket=None, speed=None,
                                       dry_run=True))
    assert tried == ['x']


def test_a_shutdown_on_one_motor_stops_the_whole_report(monkeypatch):
    # a Klipper in shutdown fails the next motor too: not a per-motor skip
    from argparse import Namespace

    from chopper_autotune.collect import KlipperShutdown
    tried = []

    def demo(kl, args):
        tried.append(args.axis)
        raise KlipperShutdown('Klipper shut down (drv_err): the run stops here')
    monkeypatch.setattr(demo_module, 'demo', demo)
    monkeypatch.setattr(demo_module, 'Klippy', lambda path: type('K', (), {
        'connect': lambda self: self, 'close': lambda self: None,
        'settings': lambda self: {'stepper_x': {}, 'stepper_y': {}}})())
    monkeypatch.setattr(demo_module, 'find_socket', lambda explicit=None: '<sock>')
    with pytest.raises(KlipperShutdown):
        demo_module.run_demo(Namespace(axis='xy', report=True, socket=None, speed=None,
                                       dry_run=True))
    assert tried == ['x']


def test_the_defaults_round_takes_the_driver_stock_tpfd_and_its_limits():
    from argparse import Namespace

    from chopper_autotune import tmc
    from chopper_autotune.collect import RunStopped
    assert demo_module.before_registers(Namespace(default=None), tmc.DRIVERS['2240']) \
        == tmc.DRIVERS['2240'].default
    # a Chopper without tpfd would leave the tuned tpfd in the chip for the DEFAULTS set
    assert demo_module.before_registers(Namespace(default=tmc.Chopper(2, 3, 5, 2)),
                                        tmc.DRIVERS['5160']) == tmc.Chopper(2, 3, 5, 2, 4)
    assert demo_module.before_registers(Namespace(default=tmc.Chopper(2, 3, 5, 0)),
                                        tmc.DRIVERS['2209']) == tmc.Chopper(2, 3, 5, 0)
    # TMC2660 refuses hstrt + hend above 15 at config load: no demo on it either
    with pytest.raises(RunStopped, match='DEFAULT=tbl2_toff4_hstrt7_hend9: hstrt \\+ hend'):
        demo_module.before_registers(Namespace(default=tmc.Chopper(2, 4, 7, 9)), tmc.DRIVERS['2660'])

