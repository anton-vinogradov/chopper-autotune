"""CHOPPER_DEMO on two motors on one axis (AWD, a two-motor gantry, #129), on printers built
from each release's own modules (tests/klipper_front.py): the report and the live show of
one motor, and the show of both, move each rail by G1 as COLLECT does, play the defaults
and the tuned set on every driver of it, home only on the drivers' own registers, and
refuse what would spoil the demo before anything moves."""
import json
import re
import types
from dataclasses import replace

import pytest

import klipper_front
from chopper_autotune import collect, demo, find_speed, rail, tmc
from chopper_autotune.cli import build_parser
from chopper_autotune.dataset import Dataset
from test_klipper_front import (AT_RUNTIME, AWD, SOURCES, assert_clean, awd_cfg, limits_in_force,
                                require)
from test_rail import (NO_TRIGGER, approach, chips, dual_y, failing_closing_homing, from_homing,
                       measured, off_the_edges, option_out, own_cfg, rail_sections, refusing,
                       running_client)

TUNED = {'x': {'toff': 4, 'hend': 3}, 'y': {'toff': 5, 'hend': 2}}
AUTOTUNE = ''.join('\n[autotune_tmc %s]\nmotor: ldo-42sth48-2004ac\n' % name
                   for name in ('stepper_x', 'stepper_x1'))


@pytest.fixture(autouse=True)
def printer_time(monkeypatch):
    monkeypatch.setattr(collect, 'time', klipper_front.Clock())


def tuned_cfg(cfg: str, driver: str, axes: str = 'xy') -> str:
    """cfg with every driver of motor A on TOFF 4, HEND 3 and every driver of motor B on
    TOFF 5, HEND 2 (of the motors in `axes`), each in stealthChop: the set the demo plays
    against the defaults, another on each rail. A section cfg has not stays out."""
    for axis in axes:
        lines = ''.join('driver_%s: %d\n' % (name.upper(), value)
                        for name, value in TUNED[axis].items())
        for name in ('stepper_' + axis, 'stepper_%s1' % axis):
            section = '[tmc%s %s]\n' % (driver, name)
            cfg = cfg.replace(section, section + 'stealthchop_threshold: 999999\n' + lines)
    return cfg


def played_sets(driver: str) -> dict:
    """What each rail plays: Klipper defaults, then its tuned set (tuned_cfg)."""
    stock = tmc.DRIVERS[driver].default
    return {axis: {'default': stock.fields(), 'tuned': replace(stock, **TUNED[axis]).fields()}
            for axis in 'xy'}


def run_demo(front, monkeypatch, argv: 'list[str]', kl=None) -> int:
    """CHOPPER_DEMO through its entry, on our client (`kl`) on the printer."""
    kl = kl or front.connect()
    monkeypatch.setattr(demo, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(demo, 'find_socket', lambda explicit=None: '<front>')
    return demo.run_demo(build_parser().parse_args(['demo'] + argv))


def demo_root():
    """The demo's dataset: it takes no DATASET=, the run makes the one there is."""
    roots = list((collect.RESULTS_HOME / 'datasets').iterdir())
    assert len(roots) == 1
    return roots[0]


def show_moves(front, speeds: dict) -> list:
    """The strokes of the show: head moves that run each belt at its own speed."""
    return [move for move in front.head_moves
            if all(abs(move['belts'][axis] - speeds[axis]) <= 0.5 for axis in 'xy')]


def round_of(front, move, sets: dict) -> str:
    """The set every X/Y driver held through a move, one for all of them: 'default' or
    'tuned' (each rail its own)."""
    held = []
    for axis in 'xy':
        for stepper in front.rail(axis):
            chip = front.chopper(move, stepper)
            held.append(next((name for name, fields in sets[axis].items()
                              if all(chip[key] == value for key, value in fields.items())), None))
    assert len(set(held)) == 1 and held[0], held
    return held[0]


def in_spreadcycle(front, move, driver: str) -> bool:
    field, spread, _ = tmc.DRIVERS[driver].spreadcycle_switch
    return all(front.chopper(move, stepper)[field] == spread
               for axis in 'xy' for stepper in front.rail(axis))


def gantry_off(front) -> bool:
    steppers = front.status({'stepper_enable': None})['stepper_enable']['steppers']
    return not [name for name, on in steppers.items() if on and name != 'stepper_z']


def homed(front) -> str:
    return front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes']


def bed_center(move, printer: str) -> bool:
    """A move across the bed center, the G-code offset or not."""
    return [(start + end) / 2 for start, end in zip(move['start'][:2], move['end'][:2])] \
        == pytest.approx([size / 2 for size in AWD[printer]['size']])


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', AWD)
@pytest.mark.parametrize('axis', 'xy')
@pytest.mark.parametrize('mode', [['--report', '--iterations', '2'],
                                  ['--rounds', '1', '--repeats', '1']], ids=['report', 'live'])
def test_a_demo_of_a_rail_plays_both_sets_on_every_driver_and_puts_the_printer_back(
        source, printer, axis, mode, monkeypatch, capsys):
    # the twin plays each set and spreadCycle too, the belt runs at the demo's speed, the
    # other stands, nothing moves one motor of the rail alone or switches one off; no
    # homing but the first and the closing one (a schedule's every 3 moves here); then
    # each driver has its own registers and mode back, the toolhead what a print left
    require(source)
    monkeypatch.setattr(collect, 'PARK_INTERVAL_MOVES', 3)
    driver = AWD[printer]['driver']
    front = klipper_front.Front(source, tuned_cfg(awd_cfg(printer), driver))
    front.run(AT_RUNTIME + '\nSET_GCODE_OFFSET X=5 Y=-3')
    configured, limits = chips(front), limits_in_force(front)
    assert run_demo(front, monkeypatch, ['--motor', 'a' if axis == 'x' else 'b', '--speed', '60']
                    + mode) == 0
    assert_clean(front)
    assert front.moves == []                                # no FORCE_MOVE
    assert not [line for script in front.scripts for line in script.split('\n')
                if line.startswith('SET_STEPPER_ENABLE') and line.endswith('ENABLE=0')]
    root = demo_root()
    manifest = Dataset.open(root).manifest()
    field, spread, _ = tmc.DRIVERS[driver].spreadcycle_switch
    other = 'y' if axis == 'x' else 'x'
    pairs = measured(front, root)
    assert len(pairs) == (8 if '--report' in mode else 4)
    played = set()
    for record, move in pairs:
        held = [move['chips'][section] for section in rail_sections(printer, axis)]
        assert held[0] == held[1] and held[0][field] == spread
        combo = {name: record[name] for name in ('tbl', 'toff', 'hstrt', 'hend')}
        assert {name: held[0][name] for name in combo} == combo
        played.add((record['toff'], record['hend']))
        assert move['belts'][axis] == pytest.approx(60, abs=0.5)
        assert move['belts'][other] == pytest.approx(0, abs=1e-6)
        assert move['accel'] == pytest.approx(manifest['head_accel'])
        assert move['cruise_ratio'] == 0
        assert bed_center(move, printer)
    stock = tmc.DRIVERS[driver].default
    assert played == {(stock.toff, stock.hend), (TUNED[axis]['toff'], TUNED[axis]['hend'])}
    off_the_edges(front, printer)
    own = limits['minimum_cruise_ratio']
    assert len(front.homings) == 2
    assert all(homing['chips'] == configured and homing['cruise_ratio'] == own
               for homing in front.homings)
    assert chips(front) == configured
    assert homed(front) == 'xy'
    assert limits_in_force(front) == limits
    assert (manifest['motion'], manifest['steppers']) == ('rail', ['stepper_' + axis,
                                                                   'stepper_%s1' % axis])
    out = capsys.readouterr().out
    # the floor measured with every motor holding would make the gain above it too high
    assert 'x less vibration overall' in out and 'above the' not in out
    state = json.loads((collect.RESULTS_HOME / 'state.json').read_text())
    tuned = replace(stock, **TUNED[axis]).fields()
    assert state[axis]['regs'] == '/'.join(str(value) for value in tuned.values())


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', list(AWD) + ['dual-y-meijjaa'])
@pytest.mark.parametrize('speeds', [{'x': 60, 'y': 60}, {'x': 60, 'y': 40}],
                         ids=['SPEED=60', 'known'])
def test_the_show_runs_each_rail_on_its_own_set_and_puts_the_printer_back(source, printer, speeds,
                                                                         monkeypatch):
    # one G1 of the head runs each belt at its speed; every round both rails play the same
    # round, each its own set, a motor alone on its axis too (dual Y)
    require(source)
    base = printer.replace('dual-y-', '')
    driver = AWD[base]['driver']
    front = klipper_front.Front(source, tuned_cfg(dual_y(base) if base != printer
                                                  else awd_cfg(base), driver))
    front.run('SET_GCODE_OFFSET X=5 Y=-3')
    configured, limits = chips(front), limits_in_force(front)
    argv = ['--rounds', '2', '--repeats', '1']
    if speeds['y'] == speeds['x']:
        argv += ['--speed', str(speeds['x'])]
    else:
        monkeypatch.setattr(demo, 'known_speed', lambda axis: speeds[axis])
    assert run_demo(front, monkeypatch, argv) == 0
    assert_clean(front)
    strokes = show_moves(front, speeds)
    assert len(strokes) == 8
    sets = played_sets(driver)
    rounds = []
    for move in strokes:
        rounds.append(round_of(front, move, sets))
        assert in_spreadcycle(front, move, driver)
        assert move['cruise_ratio'] == 0
        assert bed_center(move, base)
    assert rounds == ['default'] * 2 + ['tuned'] * 2 + ['default'] * 2 + ['tuned'] * 2
    off_the_edges(front, base)
    assert front.moves == []
    assert not [line for script in front.scripts for line in script.split('\n')
                if line.startswith('SET_STEPPER_ENABLE') and line.endswith('ENABLE=0')]
    assert len(front.homings) == 2 and all(homing['chips'] == configured
                                           for homing in front.homings)
    assert chips(front) == configured
    assert limits_in_force(front) == limits
    assert homed(front) == 'xy'
    state = json.loads((collect.RESULTS_HOME / 'state.json').read_text())
    assert {axis: state[axis]['regs'] for axis in 'xy'} == {
        axis: '/'.join(str(value) for value in sets[axis]['tuned'].values()) for axis in 'xy'}


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', ['meijjaa', 'voron-5160'])
def test_a_failed_stroke_of_the_show_re_homes_on_the_drivers_own_registers(source, printer,
                                                                          monkeypatch, capsys):
    # the first stroke of the defaults fails: a re-home on the drivers' own registers and
    # the printer's limits, then both rails play the defaults again in spreadCycle, rail B
    # too
    require(source)
    monkeypatch.setattr(collect, 'PARK_INTERVAL_MOVES', 3)
    driver = AWD[printer]['driver']
    front = klipper_front.Front(source, tuned_cfg(awd_cfg(printer), driver))
    configured, accel = chips(front), limits_in_force(front)['max_accel']
    kl = running_client(front, {1})
    assert run_demo(front, monkeypatch, ['--speed', '60', '--rounds', '1', '--repeats', '2'],
                    kl) == 0
    assert_clean(front)
    assert capsys.readouterr().out.count("Re-homing on the drivers' own registers") == 1
    assert len(front.homings) == 3                  # the first, before the second stroke, the last
    own = limits_in_force(front)['minimum_cruise_ratio']
    for homing in front.homings:
        assert (homing['axes'], homing['accel'], homing['chips'], homing['cruise_ratio']) \
            == ('xy', accel, configured, own)
    strokes = show_moves(front, {'x': 60, 'y': 60})
    assert [round_of(front, move, played_sets(driver)) for move in strokes] \
        == ['default'] * 4 + ['tuned'] * 4
    assert all(in_spreadcycle(front, move, driver) for move in strokes)


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('sync', [False, True])
@pytest.mark.parametrize('when', [1, 3], ids=['the move to the start', 'the stroke'])
def test_the_show_stops_when_x_and_y_go_off_and_hands_the_gantry_over(source, sync, when,
                                                                     monkeypatch):
    # M84 forgets the homing: the motors of a rail may have settled apart while off, so no
    # closing G28. Before the first stroke the move to its start fails; before the third
    # Klipper skips that move (the head is there already), and the stroke fails
    require(source)
    front = klipper_front.Front(source, tuned_cfg(awd_cfg('meijjaa'), '5160')
                                + '\n[motors_sync]\naxes: x,y\n' * sync)
    configured = chips(front)
    kl = front.connect()
    send, approaches = kl.gcode, []

    def gcode(script):
        if approach(script):
            approaches.append(script)
            if len(approaches) == when:
                front.run('M84')                    # the user's Disable motors
        return send(script)
    kl.gcode = gcode
    with pytest.raises(rail.GantryUnhomed, match='home X and Y again') as stopped:
        run_demo(front, monkeypatch, ['--speed', '60', '--rounds', '2', '--repeats', '1'], kl)
    assert ('SYNC_MOTORS' in str(stopped.value)) == sync
    assert chips(front) == configured
    assert len(front.homings) == 1
    assert gantry_off(front) and homed(front) == ''


@pytest.mark.parametrize('source', SOURCES)
def test_strokes_of_the_show_that_fail_in_a_row_stop_it(source, monkeypatch):
    # a stream that stalls from the third stroke on: a re-home before each next one, the
    # fourth failure in a row ends the show on the drivers' own registers
    require(source)
    front = klipper_front.Front(source, tuned_cfg(awd_cfg('meijjaa'), '5160'))
    configured = chips(front)
    kl = running_client(front, range(3, 1003))
    with pytest.raises(collect.RunStopped, match='fix what fails, then run again: 4 moves of '
                                                 'motors A and B failed in a row') as stopped:
        run_demo(front, monkeypatch, ['--speed', '60', '--rounds', '2', '--repeats', '1'], kl)
    assert collect.failure_display('demo FAILED: %s' % stopped.value)[:16] == 'FAIL fix what fa'
    assert len(front.homings) == 5
    assert all(homing['chips'] == configured for homing in front.homings)
    assert chips(front) == configured
    assert homed(front) == 'xy'


@pytest.mark.parametrize('source', SOURCES)
def test_a_driver_error_mid_show_stops_it_at_the_next_command(source, monkeypatch, capsys):
    # #133: a shutdown answers every command with the same error; no re-home, no next stroke
    require(source)
    front = klipper_front.Front(source, tuned_cfg(awd_cfg('meijjaa'), '5160'))
    chip = front.chips[rail_sections('meijjaa', 'y')[1]]
    queued = front.trapq_append

    def trapq_append(*args):
        queued(*args)
        if len(front.head_moves) == 3:              # to the center, to the start, the first stroke
            chip.reads['DRV_STATUS'] = chip.fields.all_fields['DRV_STATUS']['s2ga']
    front.toolhead.trapq_append = trapq_append
    with pytest.raises(collect.KlipperShutdown):
        run_demo(front, monkeypatch, ['--speed', '60', '--rounds', '1', '--repeats', '1'])
    assert len(front.head_moves) == 3 and len(front.printer.shutdowns) == 1
    out = capsys.readouterr().out
    assert 'sweep failed' not in out and 'Re-homing' not in out


def hybrid() -> str:
    return awd_cfg('voron-2209').replace('kinematics: corexy', 'kinematics: hybrid_corexy')


def tuned_meijjaa(max_velocity: int = 500) -> str:
    return tuned_cfg(awd_cfg('meijjaa').replace('max_velocity: 500', 'max_velocity: %d'
                                                % max_velocity), '5160')


REPORT_A, REPORT_AB = ['--motor', 'a', '--report'], ['--report']
REFUSALS = {
    # (the printer.cfg, the argv beyond --speed 60, what the display starts with)
    'registers apart, report': (lambda: own_cfg('meijjaa'), REPORT_A, 'give every drive'),
    'registers apart, show': (lambda: own_cfg('meijjaa'), [], 'give every drive'),
    'z, report of both': (tuned_meijjaa, REPORT_AB, 'raise Z to 5 mm '),
    # motor A alone on its axis: no rail there, and it would run after motor B's skip
    'z, report of both, dual Y': (lambda: tuned_cfg(dual_y('meijjaa'), '5160'), REPORT_AB,
                                  'raise Z to 5 mm '),
    'z, show': (tuned_meijjaa, [], 'raise Z to 5 mm '),
    'max_velocity, report': (lambda: tuned_meijjaa(100), REPORT_A + ['--speed', '120'],
                             'raise max_veloci'),
    'max_velocity, show': (lambda: tuned_meijjaa(100), ['--speed', '80'], 'raise max_veloci'),
    'accel, show': (tuned_meijjaa, ['--accel', '10'], 'even a 0.40s cru'),
    'hybrid_corexy, report': (hybrid, REPORT_A, 'not on hybrid_co'),
    'hybrid_corexy, report of both': (hybrid, REPORT_AB, 'not on hybrid_co'),
    'hybrid_corexy, show': (hybrid, [], 'not on hybrid_co'),
}


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('case', REFUSALS)
def test_what_would_spoil_a_rail_demo_is_refused_before_any_gcode(source, case, monkeypatch):
    # a refusal of both motors' report at its entry, not 'motor A skipped' and 'motor B
    # skipped' after it
    require(source)
    cfg, argv, shown = REFUSALS[case]
    front = klipper_front.Front(source, cfg())
    if case.startswith('z'):
        front.run('G28 Z')                              # Z homed on the bed
    sent = len(front.scripts)
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, ['--speed', '60'] + argv)
    assert front.scripts[sent:] == []
    message = refused.value.code
    assert isinstance(message, str)
    display = collect.failure_display('demo FAILED: %s' % message)
    assert display.startswith('FAIL ' + shown) and display == collect.display_text(display)
    if case == 'max_velocity, show':
        assert 'it caps the head at 100 of the 113 mm/s' in message
    if case.startswith('registers apart'):
        assert message.endswith('Copy the driver_* lines of one section into the others and '
                                'restart Klipper. Or run CHOPPER_SAVE MOTOR=A: it writes the '
                                'latest tuning result of motor A into the section of each '
                                'driver. Nothing was moved')


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', AWD)
@pytest.mark.parametrize('argv', [['--motor', 'a', '--report'], []], ids=['report', 'show'])
def test_a_dry_run_of_the_demo_says_how_the_rail_moves_and_sends_no_gcode(source, printer, argv,
                                                                          monkeypatch, capsys):
    require(source)
    front = klipper_front.Front(source, tuned_cfg(awd_cfg(printer), AWD[printer]['driver']))
    assert run_demo(front, monkeypatch, ['--speed', '60', '--dry-run'] + argv) == 0
    assert front.scripts == []
    out = capsys.readouterr().out
    for said in ('head: F', 'M204 S', 'nearest a bed edge', 'minimum_cruise_ratio',
                 'Every X/Y motor holds under current', 'note: Z is not homed'):
        assert said in out
    if argv:
        assert 'stepper_x, stepper_x1' in out
    else:
        for stepper in ('stepper_x', 'stepper_x1', 'stepper_y', 'stepper_y1'):
            assert '\n  %s: ' % stepper in out


@pytest.mark.parametrize('source', SOURCES)
def test_a_dry_run_of_the_show_warns_of_what_runs_the_drivers_of_each_motor_unlike(
        source, monkeypatch, capsys):
    # motor B's drivers as motor A's: a homing at different currents can leave them apart
    # before the first stroke
    require(source)
    front = klipper_front.Front(source, tuned_cfg(awd_cfg('meijjaa'), '5160').replace(
        '[tmc5160 stepper_y1]\n', '[tmc5160 stepper_y1]\nhold_current: 0.5\nhome_current: 0.6\n'))
    assert run_demo(front, monkeypatch, ['--speed', '60', '--dry-run']) == 0
    out = capsys.readouterr().out
    assert 'WARNING: the drivers of motor B differ in hold_current (stepper_y ' in out
    # Kalico records home_current's default, the run_current
    assert re.search(r'WARNING: the drivers of motor B home at different currents \(Kalico '
                     r'home_current: stepper_y (its run_current|0\.8 A), stepper_y1 0\.6 A\)', out)
    assert 'drivers of motor A' not in out
    assert front.scripts == []


LIMITED = {
    # the head runs below the limits it has itself: Kalico would cut one belt without an
    # error, and the show would play off resonance
    'limited_cartesian': ('meijjaa', {'kinematics: cartesian': 'kinematics: limited_cartesian\n'
                                                                'max_y_velocity: 50'},
                          {'x': 60, 'y': 60},
                          'raise max_y_velocity to 60 or more: now 50, it caps motor B at 50 of the '
                          '60 mm/s the run needs'),
    'limited_corexy': ('voron-2209', {'kinematics: corexy': 'kinematics: limited_corexy',
                                      'max_velocity: 500': 'max_velocity: 90'},
                       {'x': 100, 'y': 20},
                       'raise max_velocity to 100 or more: now 90, it caps motor A at 90 of the '
                       '100 mm/s the run needs'),
}


def limited_cfg(printer: str, changes: dict) -> str:
    cfg = tuned_cfg(awd_cfg(printer), AWD[printer]['driver'])
    for old, new in changes.items():
        assert cfg.count(old) == 1
        cfg = cfg.replace(old, new)
    return cfg


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('kinematics', LIMITED)
def test_the_show_refuses_a_belt_speed_kalicos_per_axis_limits_cut(source, kinematics,
                                                                    monkeypatch):
    require(source)
    printer, changes, speeds, refusal = LIMITED[kinematics]
    front = klipper_front.Front(source, limited_cfg(printer, changes))
    monkeypatch.setattr(demo, 'known_speed', lambda axis: speeds[axis])
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, ['--dry-run'])
    assert refused.value.code == refusal + '. Nothing was moved'
    assert front.scripts == []


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('argv, said', [
    ([], '  head: F3600, M204 S300; per-axis limits (by config) give it 100\n'),
    (['--motor', 'a', '--report'],
     ' M204 S212 for 300 mm/s2 of belt; per-axis limits (by config) give it 194\n')],
    ids=['show', 'report'])
def test_a_dry_run_of_the_demo_says_what_kalicos_per_axis_limits_leave_of_its_accel(
        source, argv, said, monkeypatch, capsys):
    # the run asks the tools' max_accel/10, limited_corexy caps it by max_x_accel
    require(source)
    front = klipper_front.Front(source, limited_cfg('voron-2209', {
        'kinematics: corexy': 'kinematics: limited_corexy\nmax_x_accel: 100'}))
    assert run_demo(front, monkeypatch, ['--speed', '60', '--dry-run'] + argv) == 0
    assert said in capsys.readouterr().out


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('argv', [['--motor', 'a', '--report'], []], ids=['report', 'show'])
def test_a_stop_as_the_demo_starts_leaves_the_toolhead_as_a_print_left_it(source, argv,
                                                                         monkeypatch):
    # SIGTERM's exit (CHOPPER_STOP) landing on the demo's first status read, the display's
    require(source)
    front = klipper_front.Front(source, tuned_meijjaa())
    front.run(AT_RUNTIME)
    limits = limits_in_force(front)

    def stopped(kl):
        raise SystemExit(143)
    monkeypatch.setattr(collect, 'accepted_commands', stopped)
    with pytest.raises(SystemExit):
        run_demo(front, monkeypatch, ['--speed', '60'] + argv)
    assert limits_in_force(front) == limits


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('sync', [False, True])
def test_a_demo_report_of_both_motors_runs_the_pair_before_the_motor_alone(source, sync,
                                                                          monkeypatch, capsys):
    # the motor alone runs with every X/Y motor off: before the pair's run its motors would
    # come back on apart, a [motors_sync] lost
    require(source)
    front = klipper_front.Front(source, tuned_cfg(dual_y('meijjaa'), '5160')
                                + '\n[motors_sync]\naxes: x,y\n' * sync)
    assert run_demo(front, monkeypatch, ['--report', '--speed', '60', '--iterations', '1']) == 0
    assert_clean(front)
    pair_off = min(index for index, script in enumerate(front.scripts)
                   if re.search(r'STEPPER="stepper_y1?" ENABLE=0', script))
    assert max(index for index, script in enumerate(front.scripts)
               if script.startswith('G1 ')) < pair_off
    assert front.moves and {move['stepper'] for move in front.moves} == {'stepper_x'}
    out = capsys.readouterr().out
    assert 'note: motor B goes first, because motor A runs alone' in out
    assert ('SYNC_MOTORS again after the demo' in out) == sync
    assert out.index('Demo on motor B') < out.index('Demo on motor A')


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('case', ['tuned', 'autotune on the defaults'])
def test_a_demo_of_both_motors_stops_at_a_driver_not_put_back_before_the_next_motor_homes(
        source, case, monkeypatch):
    # stepper_x1 keeps what it played: motor B's homing would run on it. After motor A's
    # demo, or after its refusal (autotune runs the defaults on it: nothing to demo)
    require(source)
    if case == 'tuned':
        front = klipper_front.Front(source, tuned_meijjaa())
        restore = played_sets('5160')['x']['tuned']
    else:
        front = klipper_front.Front(source, tuned_cfg(awd_cfg('meijjaa'), '5160', 'y') + AUTOTUNE)
        restore = tmc.DRIVERS['5160'].default.fields()
    configured = chips(front)
    kl = front.connect()
    refusing(kl, tmc.set_fields_script('stepper_x1', restore), {1})
    with pytest.raises(rail.RegistersStuck, match='home by hand'):
        run_demo(front, monkeypatch, ['--report', '--speed', '60', '--iterations', '1'], kl)
    assert len(front.homings) == 1 and front.homings[0]['chips'] == configured
    assert not [move for move in front.head_moves
                if move['belts']['y'] and not from_homing(front, move)]


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', ['meijjaa', 'voron-5160'])
@pytest.mark.parametrize('error', [NO_TRIGGER, 'gcode/script timed out after 60s'],
                         ids=['no trigger', 'timed out'])
def test_a_demo_of_both_motors_stops_at_a_failed_closing_homing_and_hands_the_gantry_over(
        source, printer, error, monkeypatch):
    # decision 7: the motors of a belt may have settled apart while off; motor B's homing
    # and moves would run on them, with Z no longer homed to keep the head off the bed.
    # Where Klipper left the motors on, the gantry goes to the user's hands all the same
    require(source)
    front = klipper_front.Front(source, tuned_cfg(awd_cfg(printer), AWD[printer]['driver']))
    front.run('G28 Z\nG1 Z10 F600')
    configured, homings = chips(front), len(front.homings)
    kl = front.connect()
    failing_closing_homing(front, kl, error)
    with pytest.raises(rail.GantryUnhomed, match=r'home X and Y again: the final homing failed '
                                                 r'\(%s\)' % error) as stopped:
        run_demo(front, monkeypatch, ['--report', '--speed', '60', '--iterations', '1'], kl)
    assert collect.failure_display('demo FAILED: %s' % stopped.value)[:16] == 'FAIL home X and '
    assert len(front.homings) == homings + 1
    assert not [move for move in front.head_moves
                if move['belts']['y'] and not from_homing(front, move)]
    assert chips(front) == configured
    assert gantry_off(front) and homed(front) == ('' if error == NO_TRIGGER else 'z')


@pytest.mark.parametrize('source', SOURCES)
def test_a_rail_under_klipper_tmc_autotune_plays_what_it_runs_whatever_its_driver_lines(
        source, monkeypatch):
    # autotune's registers replace the config's driver_* lines: lines that differ between
    # the drivers refuse nothing before the homing, the set read live after it plays
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa').replace(
        '[tmc5160 stepper_x1]\n', '[tmc5160 stepper_x1]\ndriver_TOFF: 5\n') + AUTOTUNE)
    front.run('SET_TMC_FIELD STEPPER=stepper_x FIELD=toff VALUE=6\n'
              'SET_TMC_FIELD STEPPER=stepper_x1 FIELD=toff VALUE=6')
    found = chips(front)
    assert run_demo(front, monkeypatch, ['--motor', 'a', '--report', '--speed', '60']) == 0
    assert_clean(front)
    stock = tmc.DRIVERS['5160'].default
    assert {(record['toff'], record['hend']) for record, _ in measured(front, demo_root())} \
        == {(stock.toff, stock.hend), (6, stock.hend)}
    assert len(front.homings) == 2 and all(homing['chips'] == found for homing in front.homings)
    assert chips(front) == found


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('argv', [['--motor', 'a', '--report'], []], ids=['report', 'show'])
def test_a_rail_klipper_tmc_autotune_runs_apart_is_refused_after_the_homing(source, argv,
                                                                            monkeypatch):
    # autotune's registers are read live, after the first homing: there is no one set to
    # play against the defaults, and the drivers end on what they ran
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa') + AUTOTUNE)
    front.run('SET_TMC_FIELD STEPPER=stepper_x FIELD=toff VALUE=6\n'
              'SET_TMC_FIELD STEPPER=stepper_x1 FIELD=hend VALUE=1')
    found = chips(front)
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, ['--speed', '60'] + argv)
    assert refused.value.code == (
        'make stepper_x and stepper_x1 run the same registers under klipper_tmc_autotune, then '
        'retry: stepper_x runs tbl2_toff6_hstrt5_hend2_tpfd4, stepper_x1 runs '
        'tbl2_toff3_hstrt5_hend1_tpfd4. The demo plays one set against the defaults')
    assert collect.failure_display('demo FAILED: %s' % refused.value.code)[:16] == 'FAIL make steppe'
    assert_clean(front)
    if argv:
        assert [record['kind'] for record in Dataset.open(demo_root()).records()] == ['baseline']
    assert show_moves(front, {'x': 60, 'y': 60}) == []
    assert len(front.homings) == 2 and all(homing['chips'] == found for homing in front.homings)
    assert chips(front) == found


LIVE = tmc.Chopper(1, 5, 4, 3, 9)       # set by APPLY=1 or SET_TMC_FIELD, not in the config
STOCK = tmc.DRIVERS['5160'].default
SAVED = {axis: replace(STOCK, **TUNED[axis]) for axis in 'xy'}
SET_LIVE = {
    # (the printer.cfg, the drivers set live, the argv, the config's set)
    'motor B on the defaults': (lambda: tuned_cfg(awd_cfg('meijjaa'), '5160', 'x'),
                                ('stepper_y', 'stepper_y1'),
                                ['--motor', 'b', '--speed', '60', '--rounds', '1'], STOCK),
    'report of B on the defaults': (lambda: tuned_cfg(awd_cfg('meijjaa'), '5160', 'x'),
                                    ('stepper_y', 'stepper_y1'),
                                    ['--motor', 'b', '--speed', '60', '--report'], STOCK),
    'show, B on the defaults': (lambda: tuned_cfg(awd_cfg('meijjaa'), '5160', 'x'),
                                ('stepper_y', 'stepper_y1'), ['--speed', '60'], STOCK),
    'show, both on the defaults': (lambda: awd_cfg('meijjaa'), ('stepper_y', 'stepper_y1'),
                                   ['--speed', '60'], STOCK),
    'motor B on a set of its own': (lambda: tuned_cfg(awd_cfg('meijjaa'), '5160'), ('stepper_y',),
                                    ['--motor', 'b', '--speed', '60', '--rounds', '1'], SAVED['y']),
    # the resonance scan would end on the config's registers before the check
    'motor B with no speed to reuse': (lambda: tuned_cfg(awd_cfg('meijjaa'), '5160'),
                                       ('stepper_y', 'stepper_y1'), ['--motor', 'b', '--rounds', '1'],
                                       SAVED['y']),
    'show, A set live': (lambda: tuned_cfg(awd_cfg('meijjaa'), '5160'),
                         ('stepper_x', 'stepper_x1'), ['--speed', '60'], SAVED['x']),
    'show, dual Y, the motor alone set live': (lambda: tuned_cfg(dual_y('meijjaa'), '5160'),
                                               ('stepper_x',), ['--speed', '60'], SAVED['x']),
}


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('case', SET_LIVE)
def test_a_demo_of_drivers_set_live_unlike_the_config_is_refused_before_anything_moves(
        source, case, monkeypatch):
    # the demo plays the config's registers and puts them back at its end: it would play
    # the wrong set and undo the live one
    require(source)
    cfg, steppers, argv, config = SET_LIVE[case]
    front = klipper_front.Front(source, cfg())
    front.run('\n'.join(tmc.set_fields_script(stepper, LIVE.fields()) for stepper in steppers))
    live = chips(front)
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, argv)
    drivers = ' and '.join(steppers)
    assert refused.value.code == (
        'save or drop the registers set live, then retry: %s %s %s, but the config that Klipper loaded '
        'gives %s %s. The demo plays the registers of that config. At its end it puts them back, '
        'which would undo the ones set live. To keep them, repeat the CHOPPER_ANALYZE that set '
        'them, with SAVE=1 in place of APPLY=1. Or write them into printer.cfg and restart Klipper '
        '(RESTART). To drop them, restart Klipper. Nothing was moved'
        % (drivers, 'run' if len(steppers) > 1 else 'runs', LIVE.label(),
           'them' if len(steppers) > 1 else 'it', config.label()))
    assert collect.failure_display('demo FAILED: %s' % refused.value.code)[:16] == 'FAIL save or dro'
    assert_clean(front)
    assert (front.homings, front.head_moves, front.moves) == ([], [], [])
    assert chips(front) == live


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('dry_run', [True, False], ids=['dry run', 'run'])
@pytest.mark.parametrize('argv, motors', [(['--motor', 'b'], 'y'), ([], 'xy')],
                         ids=['motor B', 'show'])
def test_a_demo_of_a_rail_on_the_defaults_names_where_they_come_from(source, dry_run, argv, motors,
                                                                     monkeypatch):
    # a config edited without a restart counts as the one Klipper loaded: the drivers run it
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa'))
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, ['--speed', '60'] + argv + ['--dry-run'] * dry_run)
    stock = tmc.DRIVERS['5160'].default.label()
    assert refused.value.code == (
        'tune and save %s first: the config that Klipper loaded gives %s. Both rounds would play the '
        'same registers. If you edited printer.cfg after Klipper started, restart it (RESTART). '
        'Nothing was moved'
        % ('motor B' if motors == 'y' else 'motors A and B', ', and '.join(
            'the Klipper defaults %s to stepper_%s and stepper_%s1' % (stock, axis, axis)
            for axis in motors)))
    assert collect.failure_display('demo FAILED: %s' % refused.value.code)[:16] == 'FAIL tune and sa'
    # the drivers read before the refusal (between the fences of the output), and nothing else
    assert [line for script in front.scripts for line in script.split('\n')
            if not line.startswith('ECHO CHOPPER-')] == ([] if dry_run else [
        'DUMP_TMC STEPPER=%s REGISTER=CHOPCONF' % stepper for axis in motors
        for stepper in ('stepper_' + axis, 'stepper_%s1' % axis)])


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('dry_run', [True, False], ids=['dry run', 'run'])
@pytest.mark.parametrize('argv, motors', [(['--motor', 'b'], 'y'), ([], 'xy')],
                         ids=['motor B', 'show'])
def test_a_default_equal_to_the_saved_set_asks_for_another_default(source, dry_run, argv, motors,
                                                                    monkeypatch):
    # both motors tuned and saved to motor B's set: DEFAULT= that set plays it in both rounds
    require(source)
    monkeypatch.setitem(TUNED, 'x', TUNED['y'])
    front = klipper_front.Front(source, tuned_cfg(awd_cfg('meijjaa'), '5160'))
    played = SAVED['y']
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, ['--speed', '60', '--default', '2,5,5,2'] + argv
                 + ['--dry-run'] * dry_run)
    assert refused.value.code == (
        'pass another DEFAULT= or drop it, then retry: the config that Klipper loaded gives %s. Both '
        'rounds would play the same registers. Nothing was moved'
        % ', and '.join('the DEFAULT= registers %s to stepper_%s and stepper_%s1'
                        % (played.label(), axis, axis) for axis in motors))
    assert collect.failure_display('demo FAILED: %s' % refused.value.code)[:16] == 'FAIL pass anothe'
    assert (front.homings, front.head_moves, front.moves) == ([], [], [])


@pytest.mark.parametrize('source', SOURCES)
def test_the_show_with_a_default_equal_to_one_saved_set_names_it(source, monkeypatch, capsys):
    # motor A changes; motor B plays its saved set, the one DEFAULT= gives, in both rounds
    require(source)
    front = klipper_front.Front(source, tuned_cfg(awd_cfg('meijjaa'), '5160'))
    assert run_demo(front, monkeypatch, ['--speed', '60', '--default', '2,5,5,2', '--dry-run']) == 0
    assert ('Motor B plays the same registers in both rounds: the config that Klipper loaded '
            'gives the DEFAULT= registers %s to stepper_y and stepper_y1. Pass another DEFAULT= '
            'or drop it to hear motor B change too\n'
            % SAVED['y'].label()) in capsys.readouterr().out


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('dry_run', [True, False], ids=['dry run', 'run'])
@pytest.mark.parametrize('argv, refusal', [
    (['--motor', 'b'], 'current registers equal the defaults — nothing to demo (tune and save the '
                       'motor first)'),
    ([], 'current registers equal the defaults on both motors — tune and save first')],
    ids=['motor B', 'show'])
def test_a_demo_without_a_rail_on_the_defaults_refuses_as_main_does(source, dry_run, argv, refusal,
                                                                    monkeypatch):
    # decision 19: no driver read, main's words
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa', twins=False))
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, ['--speed', '60'] + argv + ['--dry-run'] * dry_run)
    assert refused.value.code == refusal
    assert not [line for script in front.scripts for line in script.split('\n')
                if not line.startswith('ECHO CHOPPER-')]


@pytest.mark.parametrize('source', SOURCES)
def test_a_demo_of_a_motor_alone_scans_without_reading_its_drivers(source, monkeypatch):
    # decision 19: the drivers read before the resonance scan are a rail's
    require(source)
    front = klipper_front.Front(source, tuned_cfg(awd_cfg('meijjaa', twins=False), '5160'))

    def scan(kl, args, popup=True):
        raise SystemExit('scanned')
    monkeypatch.setattr(find_speed, 'scan', scan)
    with pytest.raises(SystemExit, match='^scanned$'):
        run_demo(front, monkeypatch, ['--motor', 'b', '--rounds', '1'])
    assert not [line for script in front.scripts for line in script.split('\n')
                if line.startswith('DUMP_TMC')]


@pytest.mark.parametrize('source', SOURCES)
def test_the_show_says_which_motor_plays_the_same_registers_in_both_rounds(source, monkeypatch,
                                                                           capsys):
    # motor A tuned and saved, motor B on Klipper defaults: the panel shows no gain for B
    require(source)
    front = klipper_front.Front(source, tuned_cfg(awd_cfg('meijjaa'), '5160', 'x'))
    assert run_demo(front, monkeypatch, ['--speed', '60', '--rounds', '1', '--repeats', '1']) == 0
    assert_clean(front)
    out = capsys.readouterr().out
    assert ('Motor B plays the same registers in both rounds: the config that Klipper loaded '
            'gives the Klipper defaults tbl2_toff3_hstrt5_hend2_tpfd4 to stepper_y and '
            'stepper_y1. Tune and save motor B to hear it change too\n') in out
    assert re.search(r'both motors together: \d\.\d\dx less vibration overall; motor B played the '
                     r'same registers in both rounds\n', out)
    assert [line for line in front.console if line.startswith('echo: Chopper: both motors ')][-1] \
        .endswith('x less vibration; motor B played the same registers in both rounds')
    state = json.loads((collect.RESULTS_HOME / 'state.json').read_text())
    assert state['y']['quieter'] is None and state['x']['quieter'] > 1


@pytest.mark.parametrize('source', SOURCES)
def test_registers_applied_to_a_rail_play_in_the_demo_once_saved(source, monkeypatch):
    # meijjaa's way: the search on motor B, APPLY=1, the demo, the save it names
    from test_rail import GRID_WINNER, analyze_through, collect_motor, held
    require(source)
    mk = klipper_front.FrontMoonraker(source, tuned_cfg(awd_cfg('meijjaa'), '5160', 'x'))
    collect_motor(mk, 'y')
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1')
    argv = ['--motor', 'b', '--speed', '60', '--rounds', '1', '--repeats', '1']
    with pytest.raises(SystemExit, match='^save or drop the registers set live'):
        run_demo(mk.front, monkeypatch, argv)
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], GRID_WINNER) == GRID_WINNER
    analyze_through(mk, monkeypatch, told[0])
    assert mk.restarts == 1
    assert run_demo(mk.front, monkeypatch, argv) == 0
    assert_clean(mk.front)
    assert {(record['toff'], record['hend']) for record, _ in measured(mk.front, [
        root for root in (collect.RESULTS_HOME / 'datasets').iterdir() if '_demo_' in root.name][0])} \
        == {(3, 2), (GRID_WINNER['toff'], GRID_WINNER['hend'])}
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], GRID_WINNER) == GRID_WINNER


@pytest.mark.parametrize('source', SOURCES)
def test_a_twin_switched_off_without_an_enable_pin_of_its_own_is_no_register_set_live(
        source, monkeypatch):
    # Klipper switches such a driver off with toff 0 and puts the config's toff back when the
    # motor goes on: the demo plays as the config says
    require(source)
    front = klipper_front.Front(source, option_out(tuned_cfg(awd_cfg('meijjaa'), '5160'),
                                                   'stepper_y1', 'enable_pin'))
    front.run('M84')
    assert front.chips['tmc5160 stepper_y1'].field('toff') == 0
    assert run_demo(front, monkeypatch, ['--motor', 'b', '--speed', '60', '--rounds', '1',
                                         '--repeats', '1']) == 0
    assert_clean(front)
