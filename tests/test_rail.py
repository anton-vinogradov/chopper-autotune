"""Two motors on one axis (AWD, a two-motor gantry, #129) on printers built from each
release's own modules (tests/klipper_front.py): COLLECT, FIND_SPEED and TUNE move the
rail by G1 with every driver of it on the same registers, keep off the bed edges, re-home
only on the drivers' own registers, and refuse what would spoil the run before anything
moves; a save writes the winner into every section of the rail, through FrontMoonraker."""
import json
import math
import re
import types

import pytest

import klipper_front
from chopper_autotune import analyze, collect, find_speed, rail, tmc
from chopper_autotune.cli import build_parser
from chopper_autotune.dataset import Dataset
from chopper_autotune.klippy import KlippyError
from test_klipper_front import (AT_RUNTIME, AWD, SOURCES, assert_clean, awd_cfg, limits_in_force,
                                require, run_tool)
from test_next_step import pasted

RUNS = {
    'collect': (collect.collect, ['collect', '--speed', '60', '--tbl', '1:1', '--toff', '3:4',
                                  '--hstrt', '4:4', '--hend', '3:3', '--iterations', '1', '--yes',
                                  '--no-raw']),
    'find-speed': (find_speed.scan, ['find-speed', '--min-speed', '50', '--max-speed', '70',
                                     '--step', '10', '--yes', '--no-raw']),
}
MESH = '\n[bed_mesh]\nmesh_min: 20, 20\nmesh_max: 280, 190\n\n[bed_mesh default]\nversion: 1\n'
OWN = {'stepper_x': 'driver_TOFF: 3\ndriver_HEND: 2\n', 'stepper_x1': 'driver_TOFF: 5\ndriver_HEND: 6\n',
       'stepper_y': 'driver_TOFF: 3\ndriver_HEND: 2\n', 'stepper_y1': 'driver_TOFF: 5\ndriver_HEND: 6\n'}


@pytest.fixture(autouse=True)
def printer_time(monkeypatch):
    monkeypatch.setattr(collect, 'time', klipper_front.Clock())


def own_cfg(printer: str, **extra) -> str:
    """The AWD printer with each driver in stealthChop on registers of its own: a twin's
    other than its main motor's, so a run that misses one shows."""
    cfg = awd_cfg(printer, **extra)
    driver = AWD[printer]['driver']
    for name, base in OWN.items():
        section = '[tmc%s %s]\n' % (driver, name)
        cfg = cfg.replace(section, section + 'stealthchop_threshold: 999999\n' + base)
    return cfg


def option_out(cfg: str, section: str, option: str) -> str:
    """cfg without `option` in [section]."""
    head, _, rest = cfg.partition('[%s]\n' % section)
    body, gap, tail = rest.partition('\n\n')
    return head + '[%s]\n' % section + '\n'.join(line for line in body.split('\n')
                                                 if not line.startswith(option + ':')) + gap + tail


def rail_sections(printer: str, axis: str) -> 'list[str]':
    return ['tmc%s stepper_%s%s' % (AWD[printer]['driver'], axis, twin) for twin in ('', '1')]


def edges(printer: str) -> 'list[tuple[float, float]]':
    """Each axis's bed range less the edge a rail keeps (rail.EDGE_MM, rail.EDGE_SHARE)."""
    keep = [max(rail.EDGE_MM, rail.EDGE_SHARE * size) for size in AWD[printer]['size']]
    return [(edge, size - edge) for size, edge in zip(AWD[printer]['size'], keep)]


def measured(front, root) -> 'list[tuple[dict, dict]]':
    """Each measurement of the dataset with the toolhead move it was cut from: the one whose
    cruise holds the record's steady window."""
    pairs = []
    for record in Dataset.open(root).records():
        if record.get('kind') in ('move', 'speed') and record.get('status') == 'ok':
            low, high = record['steady']
            pairs.append((record, next(move for move in front.head_moves
                                       if move['cruise'][0] <= low and high <= move['cruise'][1])))
    return pairs


def off_the_edges(front, printer: str):
    """Every toolhead move inside the kept edges, but the one from a homing to the center."""
    box = edges(printer)
    for index, move in enumerate(front.head_moves):
        points = [move['end']] + ([move['start']] if index and not from_homing(front, move) else [])
        for point in points:
            assert all(low - 1e-6 <= at <= high + 1e-6 for at, (low, high) in zip(point, box)), move


def from_homing(front, move) -> bool:
    """A move starting where G28 leaves the head: on each X/Y endstop."""
    stops = [front.fileconfig.getfloat('stepper_' + axis, 'position_endstop') for axis in 'xy']
    return all(abs(at - stop) < 1e-6 for at, stop in zip(move['start'], stops))


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', AWD)
@pytest.mark.parametrize('run', RUNS)
@pytest.mark.parametrize('axis', 'xy')
def test_a_rail_run_moves_the_rail_by_g1_with_every_driver_on_the_same_registers(
        source, printer, run, axis, tmp_path):
    # the twin gets the candidate and spreadCycle too, the belt runs at the dataset's speed
    # in the window, the other belt stands, no move reaches the edges, nothing moves one
    # motor of the rail alone; after it each driver has its own registers and mode back,
    # and the toolhead what a print left
    require(source)
    front = klipper_front.Front(source, own_cfg(printer))
    front.run(AT_RUNTIME + '\nSET_GCODE_OFFSET X=5 Y=-3')
    configured = {section: chip.chopper() for section, chip in front.chips.items()}
    limits = limits_in_force(front)
    tool, argv = RUNS[run]
    root = tmp_path / 'dataset'
    assert run_tool(front, tool, argv + ['--axis', axis, '--dataset', str(root)])[0] == 0
    assert_clean(front)
    assert front.moves == []                                # no FORCE_MOVE
    assert not [line for script in front.scripts for line in script.split('\n')
                if line.startswith('SET_STEPPER_ENABLE') and line.endswith('ENABLE=0')]
    field, spread, _ = tmc.DRIVERS[AWD[printer]['driver']].spreadcycle_switch
    pairs = measured(front, root)
    assert len(pairs) == len([r for r in Dataset.open(root).records() if r.get('kind') != 'baseline'])
    manifest = Dataset.open(root).manifest()
    other = 'y' if axis == 'x' else 'x'
    for record, move in pairs:
        held = [move['chips'][section] for section in rail_sections(printer, axis)]
        assert held[0] == held[1] and held[0][field] == spread
        wanted = (tmc.DRIVERS[AWD[printer]['driver']].default.fields() if run == 'find-speed'
                  else {name: record[name] for name in ('tbl', 'toff', 'hstrt', 'hend')})
        assert {name: held[0][name] for name in wanted} == wanted
        assert move['belts'][axis] == pytest.approx(record['speed'], abs=0.5)
        assert move['belts'][other] == pytest.approx(0, abs=1e-6)
        # the belt gets the dataset's accel: the head M204 S<accel/k> (decision 2)
        assert move['accel'] * math.hypot(*manifest['vector']) == pytest.approx(manifest['accel'])
        assert move['cruise_ratio'] == 0
        # across the bed center, the G-code offset or not
        assert [(start + end) / 2 for start, end in zip(move['start'][:2], move['end'][:2])] \
            == pytest.approx([size / 2 for size in AWD[printer]['size']])
    off_the_edges(front, printer)
    # decision 5: every G28 on the registers and mode the config gives each driver, and on
    # the printer's own limits: a sensorless homing with them freed stopped early (#164)
    assert len(front.homings) == 2 and all(homing['chips'] == configured for homing in front.homings)
    own = limits_in_force(front)['minimum_cruise_ratio']
    assert own and all(homing['cruise_ratio'] == own for homing in front.homings)
    woken = front.scripts.index('SET_STEPPER_ENABLE STEPPER=stepper_%s1 ENABLE=1' % axis)
    assert woken < min(index for index, script in enumerate(front.scripts)
                       if 'SET_TMC_FIELD STEPPER=stepper_%s1 ' % axis in script)
    assert {section: chip.chopper() for section, chip in front.chips.items()} == configured
    assert front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes'] == 'xy'
    assert limits_in_force(front) == limits
    assert (manifest['motion'], manifest['steppers'], manifest['noise_floor']) \
        == ('rail', ['stepper_' + axis, 'stepper_%s1' % axis], 'motors holding')


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('run', RUNS)
def test_a_stop_as_the_run_starts_leaves_the_toolhead_as_a_print_left_it(source, run, tmp_path,
                                                                        monkeypatch):
    # SIGTERM's exit (CHOPPER_STOP) landing on the run's first status read, the display's
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa'))
    front.run(AT_RUNTIME)
    limits = limits_in_force(front)

    def stopped(kl):
        raise SystemExit(143)
    monkeypatch.setattr(collect, 'accepted_commands', stopped)
    tool, argv = RUNS[run]
    with pytest.raises(SystemExit):
        run_tool(front, tool, argv + ['--axis', 'x', '--dataset', str(tmp_path / 'dataset')])
    assert limits_in_force(front) == limits


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', AWD)
@pytest.mark.parametrize('run', list(RUNS) + ['tune'])
def test_a_dry_run_says_how_the_rail_moves_and_sends_no_gcode(source, printer, run, capsys,
                                                             monkeypatch):
    from chopper_autotune import tune
    require(source)
    front = klipper_front.Front(source, awd_cfg(printer))
    if run == 'tune':
        kl = front.connect()
        monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
        monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
        assert tune.run_tune(build_parser().parse_args(['tune', '--dry-run'])) == 0
    else:
        tool, argv = RUNS[run]
        assert run_tool(front, tool, argv + ['--dry-run'])[0] == 0
    assert front.scripts == []
    out = capsys.readouterr().out
    corexy = AWD[printer]['kinematics'] == 'corexy'
    for axis in ('xy' if run == 'tune' else 'x'):
        assert 'stepper_%s, stepper_%s1' % (axis, axis) in out
        assert (('on the X%sY diagonal' % ('+' if axis == 'x' else '-')) if corexy
                else 'along %s' % axis.upper()) in out
    for said in ('head: F', 'M204 S', 'nearest a bed edge', 'minimum_cruise_ratio',
                 'Every X/Y motor holds under current', 'note: Z is not homed'):
        assert said in out
    if run != 'collect':
        assert 'extends the scan up to' in out
    # a rail tries and recommends only registers inside the ranges, as one motor does
    ranges = [line for line in out.splitlines() if line.startswith('Registers the run tries: ')]
    if run == 'collect':
        assert ranges == ['Registers the run tries: TBL=1 TOFF=3:4 HSTRT=4 HEND=3 MAX_HYSTERESIS=15']
    elif run == 'tune':
        assert len(ranges) == 2 and all(
            line.startswith('Registers the run tries: TBL=1:3 TOFF=1:8 HSTRT=0:7 HEND=0:15 ')
            and line.endswith(' MAX_HYSTERESIS=15') for line in ranges)


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('axis, accel', [('x', 200), ('y', 400)])
def test_meijjaas_low_limits_get_an_accel_the_bed_takes(source, axis, accel, tmp_path, capsys):
    # max_accel 500: its tenth puts 120 mm/s far past the bed; the least hundred that fits
    # a 1 s cruise between edges kept at 30 / 25 mm (240 / 160 mm of travel). The moves run
    # with minimum_cruise_ratio 0, the printer's 0.5 comes back
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa'))
    root = tmp_path / 'scan'
    assert run_tool(front, find_speed.scan, [
        'find-speed', '--axis', axis, '--min-speed', '100', '--max-speed', '120', '--step', '10',
        '--yes', '--no-raw', '--dataset', str(root)])[0] == 0
    assert_clean(front)
    assert 'M204 S%d for %d mm/s2 of belt' % (accel, accel) in capsys.readouterr().out
    assert Dataset.open(root).manifest()['accel'] == accel
    for record, move in measured(front, root):
        assert move['accel'] == pytest.approx(accel) and move['cruise_ratio'] == 0
    assert front.status({'toolhead': ['minimum_cruise_ratio', 'max_accel']})['toolhead'] \
        == {'minimum_cruise_ratio': .5, 'max_accel': 500}


@pytest.mark.parametrize('source', SOURCES)
def test_a_cruise_shrunk_to_the_bed_rounds_down_inside_the_kept_edges(source, tmp_path, capsys):
    # 207 mm/s on meijjaa's X takes its belt's ceiling, 500 mm/s2: 0.745 s of cruise fit the
    # 240 mm between the edges, 0.75 s would cross each by half a millimetre
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa'))
    root = tmp_path / 'grid'
    assert run_tool(front, collect.collect, RUNS['collect'][1] + [
        '--axis', 'x', '--speed', '207', '--toff', '4:4', '--dataset', str(root)])[0] == 0
    assert_clean(front)
    assert 'shrinking to 0.74s' in capsys.readouterr().out
    assert Dataset.open(root).manifest()['measure_time'] == 0.74
    off_the_edges(front, 'meijjaa')


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', ['meijjaa', 'voron-2209'])
def test_a_whole_tune_finds_the_quietest_chopper_of_each_rail(source, printer, monkeypatch, capsys):
    from chopper_autotune import tune
    require(source)
    front = klipper_front.Front(source, awd_cfg(printer))
    stock = {section: chip.chopper() for section, chip in front.chips.items()}
    kl = front.connect()
    monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
    assert tune.run_tune(build_parser().parse_args(['tune', '--no-raw'])) == 0
    assert_clean(front)
    winners = {}
    for root in sorted((collect.RESULTS_HOME / 'datasets').iterdir()):
        manifest = json.loads((root / 'manifest.json').read_text())
        assert manifest['motion'] == 'rail'
        if manifest.get('winner'):
            winner = manifest['winner']
            winners[tuple(manifest['steppers'])] = (winner['toff'], winner['hend'])
    assert winners == {('stepper_x', 'stepper_x1'): (4, 3), ('stepper_y', 'stepper_y1'): (4, 3)}
    # the one way to keep both rails, each whole
    assert capsys.readouterr().out.splitlines()[-1] == 'To save: CHOPPER_SAVE MOTOR=AB'
    assert front.moves == []
    assert front.homings and all(homing['chips'] == stock for homing in front.homings)
    assert {section: chip.chopper() for section, chip in front.chips.items()} == stock


@pytest.mark.parametrize('source', SOURCES)
def test_speed_names_one_motor_on_a_two_motor_axis_too(source, monkeypatch, capsys):
    # each rail has its own resonance: SPEED= without MOTOR= is refused before any G-code,
    # and with it the tune of that rail takes it, no speed scan first
    from chopper_autotune import tune
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa'))
    kl = front.connect()
    monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
    with pytest.raises(SystemExit, match=re.escape(
            'add MOTOR=A or B: each motor has its own resonance speed. For example: '
            'CHOPPER_TUNE MOTOR=A SPEED=60')):
        tune.run_tune(build_parser().parse_args(['tune', '--speed', '60']))
    assert tune.run_tune(build_parser().parse_args(['tune', '--motor', 'b', '--speed', '60',
                                                    '--dry-run'])) == 0
    assert front.scripts == []
    out = capsys.readouterr().out
    assert 'Motor B is a rail of 2 TMC5160 drivers' in out and 'head: F3600 for 60 mm/s' in out
    assert 'extends the scan' not in out and 'Motor A is a rail' not in out


def dual_y(printer: str) -> str:
    """The AWD printer with one motor on X: two on Y alone."""
    return re.sub(r'\[(tmc\d+ )?stepper_x1\]\n.*?\n\n', '', awd_cfg(printer), flags=re.S)


def resonance_at(monkeypatch, speed: int = 60):
    """CHOPPER_TUNE of both motors without the speed scans: each motor's scan finds `speed`.
    SPEED= names the resonance of one motor alone."""
    from chopper_autotune import tune
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup=True: (0, speed, speed))


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('sync', [False, True])
def test_a_tune_of_both_motors_runs_the_pair_before_the_motor_alone(source, sync, monkeypatch,
                                                                     capsys):
    # the motor alone runs with every X/Y motor off: before the pair's run its motors would
    # come back on apart, a [motors_sync] lost
    from chopper_autotune import tune
    require(source)
    front = klipper_front.Front(source, dual_y('meijjaa') + '\n[motors_sync]\naxes: x,y\n' * sync)
    kl = front.connect()
    monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
    resonance_at(monkeypatch)
    assert tune.run_tune(build_parser().parse_args(['tune', '--no-raw'])) == 0
    assert_clean(front)
    pair_off = min(index for index, script in enumerate(front.scripts)
                   if re.search(r'STEPPER="stepper_y1?" ENABLE=0', script))
    assert max(index for index, script in enumerate(front.scripts)
               if script.startswith('G1 ')) < pair_off
    assert front.moves and {move['stepper'] for move in front.moves} == {'stepper_x'}
    runs, summary = capsys.readouterr().out.split('=== Summary ===')
    assert 'note: motor B goes first, because motor A runs alone' in runs
    assert ('SYNC_MOTORS again after the tune' in runs) == sync
    assert runs.index('=== Motor B ===') < runs.index('=== Motor A ===')
    assert summary.index('[tmc5160 stepper_x]') < summary.index('[tmc5160 stepper_y]') \
        < summary.index('[tmc5160 stepper_y1]')
    assert summary.splitlines()[-1] == 'To save: CHOPPER_SAVE MOTOR=AB'


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', ['dual Y', 'AWD'])
@pytest.mark.parametrize('sync', [False, True])
@pytest.mark.parametrize('unpeaked', ['y', 'x'], ids=['motor B', 'motor A'])
def test_a_tune_of_both_motors_goes_on_past_the_one_without_a_peak(source, printer, sync,
                                                                    unpeaked, monkeypatch, capsys):
    # without a peak on one motor the other still gets its tune, and the console names the
    # tune of the one without. On dual Y the pair runs first: motor A then runs alone with
    # every X/Y motor off, so a [motors_sync] has to sync the pair before its tune
    from chopper_autotune import tune
    require(source)
    config = dual_y('meijjaa') if printer == 'dual Y' else awd_cfg('meijjaa')
    front = klipper_front.Front(source, config + '\n[motors_sync]\naxes: x,y\n' * sync)
    kl = front.connect()
    monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup=True: (
        0, None if args.axis == unpeaked else 60, 30))
    assert tune.run_tune(build_parser().parse_args(['tune', '--no-raw'])) == 0
    assert_clean(front)
    tuned, other = ('A', 'B') if unpeaked == 'y' else ('B', 'A')
    assert [Dataset.open(root).manifest()['axis'] for root in
            (collect.RESULTS_HOME / 'datasets').iterdir()] == ['x' if unpeaked == 'y' else 'y']
    said = [line for line in front.console if 'CHOPPER_' in line]
    assert said[0].startswith('echo: Tune done: %s ' % tuned)
    assert said[0].endswith(' — to save: CHOPPER_SAVE MOTOR=%s' % tuned)
    pair_off = any(re.search(r'STEPPER="stepper_y1?" ENABLE=0', script) for script in front.scripts)
    assert pair_off == (printer == 'dual Y' and unpeaked == 'y')
    first = ('Motor A then ran alone, with every X/Y motor off, so the motors of B lost their '
             'sync. Run G28 and SYNC_MOTORS first. ' if pair_off and sync else '')
    assert said[1:] == ['Chopper: %s: no clear resonance peak. %sTo tune motor %s at the speed it '
                        'vibrates most: CHOPPER_TUNE MOTOR=%s SPEED=30 NO_RAW=1'
                        % (other, first, other, other)]
    assert capsys.readouterr().out.splitlines()[-1] == 'To save: CHOPPER_SAVE MOTOR=' + tuned


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', ['meijjaa', 'voron-2209'])
def test_a_twin_without_its_own_enable_pin_keeps_each_candidate(source, printer, tmp_path):
    # enabling such a driver puts the config's toff back: the rail stays on the whole run
    require(source)
    front = klipper_front.Front(source, option_out(awd_cfg(printer), 'stepper_x1', 'enable_pin'))
    root = tmp_path / 'grid'
    assert run_tool(front, collect.collect, [
        'collect', '--axis', 'x', '--speed', '60', '--tbl', '1:1', '--toff', '5:6', '--hstrt', '4:4',
        '--hend', '3:3', '--iterations', '1', '--yes', '--no-raw', '--dataset', str(root)])[0] == 0
    assert_clean(front)
    twin = rail_sections(printer, 'x')[1]
    pairs = measured(front, root)
    assert pairs and all(move['chips'][twin]['toff'] == record['toff'] for record, move in pairs)


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', AWD)
@pytest.mark.parametrize('sync', [False, True])
def test_a_hot_twin_stops_the_run_before_any_move_and_the_gantry_goes_off(source, printer, sync,
                                                                          tmp_path):
    # SET_STEPPER_ENABLE leaves [motors_sync]'s applied flag true: the stop says to sync
    require(source)
    front = klipper_front.Front(source, awd_cfg(printer) + '\n[motors_sync]\naxes: x,y\n' * sync)
    stock = {section: chip.chopper() for section, chip in front.chips.items()}
    chip = front.chips[rail_sections(printer, 'x')[1]]
    chip.reads['DRV_STATUS'] = chip.fields.all_fields['DRV_STATUS']['otpw']
    with pytest.raises(collect.DriverTooHot, match='stepper_x1 overheating') as stopped:
        run_tool(front, collect.collect, RUNS['collect'][1] + ['--axis', 'x', '--dataset',
                                                               str(tmp_path / 'g')])
    assert ('then SYNC_MOTORS by hand' in str(stopped.value)) == sync
    assert_clean(front)
    assert front.head_moves == [] and front.homings == []
    assert {section: chip.chopper() for section, chip in front.chips.items()} == stock
    steppers = front.status({'stepper_enable': None})['stepper_enable']['steppers']
    assert not [name for name, on in steppers.items() if on and name != 'stepper_z']


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', ['meijjaa', 'voron-2209'])
def test_a_twin_driver_error_mid_run_stops_it_at_the_next_command(source, printer, tmp_path):
    # #133: a shutdown answers every command with the same error; the way back is tried
    require(source)
    front = klipper_front.Front(source, awd_cfg(printer))
    chip = front.chips[rail_sections(printer, 'x')[1]]
    queued, shorted = front.trapq_append, []

    def trapq_append(*args):
        queued(*args)
        if len(front.head_moves) == 3:              # to the center, to the start, the first measured
            chip.reads['DRV_STATUS'] = chip.fields.all_fields['DRV_STATUS']['s2ga']
            shorted.append(len(front.scripts))
    front.toolhead.trapq_append = trapq_append
    with pytest.raises(collect.KlipperShutdown):
        run_tool(front, collect.collect, RUNS['collect'][1] + ['--axis', 'x', '--dataset',
                                                               str(tmp_path / 'g')])
    assert len(front.head_moves) == 3 and len(front.printer.shutdowns) == 1
    # the next command failed, then the way back was tried, the twin's registers too
    assert [script for script in front.scripts[shorted[0]:]
            if script.startswith('SET_TMC_FIELD STEPPER=stepper_x1 ')]


def approach(script: str) -> bool:
    """The head's way to the start of a move, before each attempt (a homing's starts with
    M204 too)."""
    return script.startswith('M204 S') and 'G28' not in script


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('when', ['noise floor', 'moves'])
def test_a_twin_hot_mid_run_stops_it_and_the_gantry_goes_off(source, when, tmp_path):
    # the rail holds under current from the first homing on: the guard watches the noise
    # floor too, and every move
    require(source)
    front = klipper_front.Front(source, own_cfg('meijjaa'))
    configured = {section: chip.chopper() for section, chip in front.chips.items()}
    chip = front.chips[rail_sections('meijjaa', 'x')[1]]
    kl = front.connect()
    send = kl.gcode

    def gcode(script):
        if script.startswith('G4 P') if when == 'noise floor' else approach(script):
            chip.reads['DRV_STATUS'] = chip.fields.all_fields['DRV_STATUS']['otpw']
        return send(script)
    kl.gcode = gcode
    try:
        with pytest.raises(collect.DriverTooHot, match='stepper_x1 overheating'):
            collect.collect(kl, build_parser().parse_args(RUNS['collect'][1] + [
                '--axis', 'x', '--dataset', str(tmp_path / 'g')]))
    finally:
        kl.close()
    # to the center; or that, to the start, the move the driver warned during
    assert len(front.head_moves) == (1 if when == 'noise floor' else 3)
    floors = [record for record in Dataset.open(tmp_path / 'g').records()
              if record['kind'] == 'baseline']
    assert len(floors) == (when == 'moves')         # the noise floor stopped midway
    assert {section: chip.chopper() for section, chip in front.chips.items()} == configured
    assert len(front.homings) == 1
    steppers = front.status({'stepper_enable': None})['stepper_enable']['steppers']
    assert not [name for name, on in steppers.items() if on and name != 'stepper_z']


def running_client(front, failing=()):
    """Our client on the printer; the captures numbered in `failing` (1 the noise floor's,
    then one an attempt) fail as a stalled stream does."""
    kl = front.connect()
    waits = kl.wait_for_sample
    count = {'waits': 0}

    def wait_for_sample(t, timeout=5.0):
        count['waits'] += 1
        if count['waits'] in failing:
            raise KlippyError('accelerometer stream stalled, no samples past %.3f' % t)
        return waits(t, timeout)
    kl.wait_for_sample = wait_for_sample
    return kl


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', ['meijjaa', 'voron-5160'])
def test_every_homing_of_a_rail_run_is_on_the_drivers_own_registers(source, printer, tmp_path,
                                                                    monkeypatch, capsys):
    # sensorless homing was tuned on the config's registers and the printer's accel: the
    # re-home after a failed attempt puts them back first, then the candidate and the run's
    # M204 again. No homing on a schedule, the single motor's every 3 moves here: a homing
    # can leave the two motors of a belt apart, and a G1 keeps the position (#164)
    require(source)
    monkeypatch.setattr(collect, 'PARK_INTERVAL_MOVES', 3)
    front = klipper_front.Front(source, own_cfg(printer))
    configured = {section: chip.chopper() for section, chip in front.chips.items()}
    accel = limits_in_force(front)['max_accel']
    kl = running_client(front, {5})                 # the first attempt of the 4th move
    root = tmp_path / 'grid'
    try:
        code, _ = collect.collect(kl, build_parser().parse_args(
            RUNS['collect'][1] + ['--axis', 'x', '--iterations', '2', '--validate', '0',
                                  '--dataset', str(root)]))
    finally:
        kl.close()
    assert code == 0
    assert_clean(front)
    # the start, before the retry of the 4th move, the end
    assert capsys.readouterr().out.count("Re-homing on the drivers' own registers") == 1
    assert len(front.homings) == 3
    own = limits_in_force(front)['minimum_cruise_ratio']
    for homing in front.homings:
        assert (homing['axes'], homing['accel'], homing['chips'], homing['cruise_ratio']) \
            == ('xy', accel, configured, own)
    field, spread, _ = tmc.DRIVERS[AWD[printer]['driver']].spreadcycle_switch
    pairs = measured(front, root)
    assert len(pairs) == 8
    assert {move['accel'] for _, move in pairs} == {Dataset.open(root).manifest()['head_accel']}
    for record, move in pairs:                      # the run's own again after each homing
        for section in rail_sections(printer, 'x'):
            assert (move['chips'][section]['toff'], move['chips'][section][field]) \
                == (record['toff'], spread)


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('sync', [False, True])
def test_x_and_y_switched_off_mid_run_stop_it_and_the_gantry_is_handed_over(source, sync, tmp_path):
    # M84 (or a failed G28) forgets the homing: every move would fail, and the motors of a
    # rail may have settled apart while off; [motors_sync] lost its sync with them
    require(source)
    front = klipper_front.Front(source, own_cfg('meijjaa') + ('\n[motors_sync]\naxes: x,y\n' * sync))
    configured = {section: chip.chopper() for section, chip in front.chips.items()}
    kl = front.connect()
    send, approaches = kl.gcode, []

    def gcode(script):
        if approach(script):
            approaches.append(script)
            if len(approaches) == 3:
                front.run('M84')                    # the user's Disable motors
        return send(script)
    kl.gcode = gcode
    try:
        with pytest.raises(rail.GantryUnhomed, match='home X and Y again') as stopped:
            collect.collect(kl, build_parser().parse_args(RUNS['collect'][1] + [
                '--axis', 'x', '--dataset', str(tmp_path / 'g')]))
    finally:
        kl.close()
    assert ('SYNC_MOTORS' in str(stopped.value)) == sync
    assert collect.failure_display('collect FAILED: %s' % stopped.value)[:16] == 'FAIL home X and '
    assert {section: chip.chopper() for section, chip in front.chips.items()} == configured
    assert len(front.homings) == 1                  # no G28 after the motors went off
    steppers = front.status({'stepper_enable': None})['stepper_enable']['steppers']
    assert not [name for name, on in steppers.items() if on and name != 'stepper_z']
    assert front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes'] == ''


@pytest.mark.parametrize('source', SOURCES)
def test_a_move_that_needs_z_homed_stops_the_run_and_the_gantry_is_handed_over(source, tmp_path):
    # a mesh loaded mid-run with Z unhomed: every move would fail the same way (decision 7)
    require(source)
    front = klipper_front.Front(source, own_cfg('meijjaa') + MESH)
    configured = {section: chip.chopper() for section, chip in front.chips.items()}
    kl = front.connect()
    send, approaches = kl.gcode, []

    def gcode(script):
        if approach(script):
            approaches.append(script)
            if len(approaches) == 3:
                front.run('BED_MESH_PROFILE LOAD=default')
        return send(script)
    kl.gcode = gcode
    try:
        with pytest.raises(rail.GantryUnhomed,
                           match=r'home all axes \(G28\), then retry') as stopped:
            collect.collect(kl, build_parser().parse_args(RUNS['collect'][1] + [
                '--axis', 'x', '--dataset', str(tmp_path / 'g')]))
    finally:
        kl.close()
    assert collect.failure_display('collect FAILED: %s' % stopped.value)[:16] == 'FAIL home all ax'
    assert {section: chip.chopper() for section, chip in front.chips.items()} == configured
    assert len(front.homings) == 1                  # the first alone
    steppers = front.status({'stepper_enable': None})['stepper_enable']['steppers']
    assert not [name for name, on in steppers.items() if on and name != 'stepper_z']
    assert front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes'] == ''


@pytest.mark.parametrize('source', SOURCES)
def test_moves_that_fail_in_a_row_stop_the_run_instead_of_re_homing_before_each(source, tmp_path):
    # a stream that stalls from the third capture on: each failed attempt re-homes first,
    # the fourth in a row ends the run on the drivers' own registers
    require(source)
    front = klipper_front.Front(source, own_cfg('meijjaa'))
    configured = {section: chip.chopper() for section, chip in front.chips.items()}
    kl = running_client(front, range(3, 1003))
    try:
        with pytest.raises(collect.RunStopped, match='fix what fails, then run again: 4 moves of '
                                                     'motor A failed in a row') as stopped:
            collect.collect(kl, build_parser().parse_args(RUNS['collect'][1] + [
                '--axis', 'x', '--dataset', str(tmp_path / 'g')]))
    finally:
        kl.close()
    assert 'accelerometer stream stalled' in str(stopped.value)
    assert collect.failure_display('collect FAILED: %s' % stopped.value)[:16] == 'FAIL fix what fa'
    # the first, a re-home before each of the 3 attempts after a failed one, the last
    assert len(front.homings) == 5
    assert all(homing['chips'] == configured for homing in front.homings)
    assert {section: chip.chopper() for section, chip in front.chips.items()} == configured
    assert front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes'] == 'xy'


@pytest.mark.parametrize('source', SOURCES)
def test_failed_attempts_apart_do_not_stop_the_run(source, tmp_path):
    # the first attempt of each move fails, its retry works: as many failures as stop a run
    # in a row, never two in a row
    require(source)
    front = klipper_front.Front(source, own_cfg('meijjaa'))
    kl = running_client(front, range(2, 2 * rail.FAILED_IN_A_ROW + 1, 2))
    root = tmp_path / 'grid'
    try:
        code, _ = collect.collect(kl, build_parser().parse_args(
            RUNS['collect'][1] + ['--axis', 'x', '--validate', '0', '--dataset', str(root)]))
    finally:
        kl.close()
    assert code == 0
    assert_clean(front)
    assert len(measured(front, root)) == rail.FAILED_IN_A_ROW
    # the first, one before each retry, the last
    assert len(front.homings) == rail.FAILED_IN_A_ROW + 2


@pytest.mark.parametrize('source', SOURCES)
def test_a_g_code_offset_set_mid_run_keeps_the_moves_across_the_bed_center(source, tmp_path):
    # Klipper adds homing_origin to every G1: the run reads it before each move
    require(source)
    front = klipper_front.Front(source, own_cfg('meijjaa'))
    kl = front.connect()
    send, strokes = kl.gcode, []

    def gcode(script):
        sent = send(script)
        if script.startswith('G1 ') and 'F6000' not in script:
            strokes.append(script)
            if len(strokes) == 2:                   # the user's, between two moves
                front.run('SET_GCODE_OFFSET X=40 Y=-30')
        return sent
    kl.gcode = gcode
    root = tmp_path / 'g'
    try:
        code, _ = collect.collect(kl, build_parser().parse_args(RUNS['collect'][1] + [
            '--axis', 'x', '--dataset', str(root)]))
    finally:
        kl.close()
    assert code == 0
    assert_clean(front)
    assert len(front.homings) == 2
    pairs = measured(front, root)
    assert len(pairs) == len([r for r in Dataset.open(root).records() if r['kind'] == 'move'])
    for record, move in pairs:
        assert [(start + end) / 2 for start, end in zip(move['start'][:2], move['end'][:2])] \
            == pytest.approx([size / 2 for size in AWD['meijjaa']['size']])
    off_the_edges(front, 'meijjaa')


OWN_X1 = {'tbl': 2, 'toff': 5, 'hstrt': 5, 'hend': 6, 'tpfd': 4}      # own_cfg's stepper_x1
FIELD, SPREAD, STEALTH = tmc.DRIVERS['5160'].spreadcycle_switch
UNWRITABLE = "gcode/script failed: Unable to write tmc spi 'stepper_x1' register CHOPCONF"


def refusing(kl, script: str, times: 'set[int]'):
    """kl, with the `times`-th sending of `script` (1 the first) failing as an unwritable
    driver does; the sendings counted in the list returned."""
    send, sent = kl.gcode, []

    def gcode(text):
        if text == script:
            sent.append(text)
            if len(sent) in times:
                raise KlippyError(UNWRITABLE)
        return send(text)
    kl.gcode = gcode
    return sent


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('write', [OWN_X1, {FIELD: STEALTH}], ids=['registers', 'mode'])
def test_a_driver_not_put_back_hands_the_gantry_over_and_fails_the_run(source, write, tmp_path,
                                                                        capsys):
    # a sensorless G28 on what may still be a candidate can home wrong (decision 8); a run
    # that ended well would let TUNE's next motor home on it
    require(source)
    front = klipper_front.Front(source, own_cfg('meijjaa'))
    kl = front.connect()
    refusing(kl, tmc.set_fields_script('stepper_x1', write), {1})
    try:
        with pytest.raises(rail.RegistersStuck, match='home by hand') as stopped:
            collect.collect(kl, build_parser().parse_args(RUNS['collect'][1] + [
                '--axis', 'x', '--dataset', str(tmp_path / 'g')]))
    finally:
        kl.close()
    assert collect.failure_display('collect FAILED: %s' % stopped.value)[:16] == 'FAIL check the X'
    assert "Not homing: a driver could not be put back" in capsys.readouterr().out
    assert len(front.homings) == 1                  # the first alone
    twin = front.chips[rail_sections('meijjaa', 'x')[1]].chopper()
    assert {name: twin[name] for name in write} != write
    steppers = front.status({'stepper_enable': None})['stepper_enable']['steppers']
    assert not [name for name, on in steppers.items() if on and name != 'stepper_z']
    assert 'x' not in front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes']


@pytest.mark.parametrize('source', SOURCES)
def test_a_tune_stops_at_a_driver_not_put_back_before_the_next_motor_homes(source, monkeypatch):
    from chopper_autotune import tune
    require(source)
    monkeypatch.setattr(collect, 'PARK_INTERVAL_MOVES', 10 ** 6)     # the closing write alone
    front = klipper_front.Front(source, own_cfg('meijjaa'))
    configured = chips(front)
    kl = front.connect()
    refusing(kl, tmc.set_fields_script('stepper_x1', OWN_X1), {1})
    monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
    resonance_at(monkeypatch)
    with pytest.raises(rail.RegistersStuck):
        tune.run_tune(build_parser().parse_args(['tune', '--no-raw']))
    assert len(front.homings) == 1 and front.homings[0]['chips'] == configured
    assert not [move for move in front.head_moves
                if move['belts']['y'] and not from_homing(front, move)]


NO_TRIGGER = 'gcode/script failed: No trigger on x after full movement'


def failing_closing_homing(front, kl, error: str = NO_TRIGGER):
    """kl, with the first closing homing failing. As a sensorless one that never triggers,
    Klipper then switches every motor off and forgets the homing (motor_off); another
    `error`, a timeout, leaves the motors as they were."""
    send = kl.gcode

    def gcode(script):
        if script == 'G28 X Y':                     # the closing one; the others go to the center
            if error == NO_TRIGGER:
                front.run('M84')
            raise KlippyError(error)
        return send(script)
    kl.gcode = gcode


@pytest.mark.parametrize('source', SOURCES)
def test_a_tune_stops_at_a_failed_closing_homing_before_the_next_motor_homes(source, monkeypatch):
    # the motors of a belt may have settled apart while off (decision 7): motor B's homing
    # and moves would run on them
    from chopper_autotune import tune
    require(source)
    front = klipper_front.Front(source, own_cfg('meijjaa'))
    kl = front.connect()
    failing_closing_homing(front, kl)
    monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
    resonance_at(monkeypatch)
    with pytest.raises(rail.GantryUnhomed, match=r'home X and Y again: the final homing failed '
                                                 r'\(gcode/script failed: No trigger on x'):
        tune.run_tune(build_parser().parse_args(['tune', '--no-raw']))
    assert len(front.homings) == 1
    assert not [move for move in front.head_moves
                if move['belts']['y'] and not from_homing(front, move)]
    steppers = front.status({'stepper_enable': None})['stepper_enable']['steppers']
    assert not [name for name, on in steppers.items() if on]
    assert front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes'] == ''


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('write, times', [
    # the drivers' own registers before the G28; the run's mode after it
    (OWN_X1, {1}), ({FIELD: SPREAD}, {2})], ids=['before the homing', 'after it'])
def test_a_write_around_a_re_home_that_fails_stops_the_run(source, write, times, tmp_path):
    # no G28 on registers unknown (decision 5), no move measured on them; the re-home is
    # the one before the retry of the 4th move
    require(source)
    front = klipper_front.Front(source, own_cfg('meijjaa'))
    configured = chips(front)
    kl = running_client(front, {5})
    refusing(kl, tmc.set_fields_script('stepper_x1', write), times)
    root = tmp_path / 'grid'
    try:
        with pytest.raises(rail.RegistersStuck, match='a register write around a re-home failed'):
            collect.collect(kl, build_parser().parse_args(
                RUNS['collect'][1] + ['--axis', 'x', '--validate', '0', '--dataset', str(root)]))
    finally:
        kl.close()
    assert len(measured(front, root)) == 3          # the moves before the re-home
    # the first, the re-home's own when the registers came back, the closing one
    assert len(front.homings) == 2 + (write != OWN_X1)
    assert all(homing['chips'] == configured for homing in front.homings)
    assert chips(front) == configured


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('loaded, back', [('default', 'default'), ('adaptive-1F', '')])
def test_a_loaded_bed_mesh_is_cleared_for_the_run_and_its_profile_loaded_back(
        source, loaded, back, tmp_path, capsys):
    # with Z unhomed a mesh fails every G1; an adaptive mesh, no saved profile, stays off
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa') + MESH)
    mesh = front.printer.objects['bed_mesh']
    mesh.loaded = loaded
    assert run_tool(front, collect.collect, RUNS['collect'][1] + [
        '--axis', 'x', '--dataset', str(tmp_path / 'g')])[0] == 0
    assert_clean(front)
    assert mesh.loaded == back
    assert front.scripts.index('BED_MESH_CLEAR') < min(
        index for index, script in enumerate(front.scripts) if script.startswith('G28'))
    out = capsys.readouterr().out
    assert 'note: the bed mesh %s is cleared for the run' % loaded in out
    assert ('no saved profile' in out) == (not back)


@pytest.mark.parametrize('source', SOURCES)
def test_a_scan_extends_no_faster_than_max_velocity_lets_g1_run(source, tmp_path, capsys):
    # G1 cuts a move to max_velocity without an error: the window would measure a slower
    # belt than the dataset says. The curve rises at 30-50, the extension stops at 70
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa').replace('max_velocity: 500',
                                                                    'max_velocity: 70'))
    root = tmp_path / 'scan'
    assert run_tool(front, find_speed.scan, [
        'find-speed', '--axis', 'x', '--min-speed', '30', '--max-speed', '50', '--step', '10',
        '--yes', '--no-raw', '--dataset', str(root)]) == (0, 60, 60)
    assert_clean(front)
    assert 'extends the scan up to 70 mm/s' in capsys.readouterr().out
    assert max(record['speed'] for record, _ in measured(front, root)) == 70


@pytest.mark.parametrize('source', SOURCES)
def test_a_scan_reaching_past_max_velocity_is_refused_before_any_gcode(source, tmp_path):
    # its slow speeds fit, G1 would cut the fast ones to max_velocity and the dataset record
    # them as asked (decision 9)
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa').replace('max_velocity: 500',
                                                                    'max_velocity: 100'))
    with pytest.raises(SystemExit, match=r'raise max_velocity .* of the 120 mm/s the run needs'):
        run_tool(front, find_speed.scan, [
            'find-speed', '--axis', 'x', '--min-speed', '50', '--max-speed', '120', '--step', '10',
            '--yes', '--no-raw', '--dataset', str(tmp_path / 'scan')])
    assert front.scripts == []


@pytest.mark.parametrize('source', SOURCES)
def test_a_rail_klipper_tmc_autotune_manages_ends_on_the_registers_it_found(source, tmp_path):
    # autotune writes its own chopper at every start, not the config's driver_* lines: each
    # driver of the rail is read live, the twin too, and every homing and the end of the run
    # put back what each ran (decisions 5 and 11)
    require(source)
    front = klipper_front.Front(source, own_cfg('meijjaa') + ''.join(
        '\n[autotune_tmc %s]\nmotor: ldo-42sth48-2004ac\n' % name
        for name in ('stepper_x', 'stepper_x1')))
    front.run('SET_TMC_FIELD STEPPER=stepper_x FIELD=toff VALUE=6\n'
              'SET_TMC_FIELD STEPPER=stepper_x1 FIELD=hend VALUE=1')
    found = chips(front)
    assert found != chips(klipper_front.Front(source, own_cfg('meijjaa')))
    assert run_tool(front, collect.collect, RUNS['collect'][1] + [
        '--axis', 'x', '--dataset', str(tmp_path / 'grid')])[0] == 0
    assert_clean(front)
    assert len(front.homings) == 2 and all(homing['chips'] == found for homing in front.homings)
    assert chips(front) == found


def mismatched_model(cfg: str) -> str:
    return re.sub(r'\[tmc5160 stepper_x1\]\ncs_pin: (\S+)\nrun_current: 0.8\nsense_resistor: 0.075',
                  r'[tmc2209 stepper_x1]\nuart_pin: \1\nrun_current: 0.8\nsense_resistor: 0.110', cfg)


def without_twin_driver(cfg: str) -> str:
    return re.sub(r'\[tmc5160 stepper_x1\]\n.*?\n\n', '', cfg, flags=re.S)


DUAL_CARRIAGE = ('\n[dual_carriage]\naxis: x\nstep_pin: P90\ndir_pin: P91\nenable_pin: !P92\n'
                 'microsteps: 16\nrotation_distance: 40\nendstop_pin: ^P93\nposition_endstop: 300\n'
                 'position_max: 300\n')
REFUSALS = {
    # (the printer.cfg, the argv beyond COLLECT's, what the display starts with)
    'driver models': (lambda: mismatched_model(awd_cfg('meijjaa')), [], 'use one driver mo'),
    'no driver section': (lambda: without_twin_driver(awd_cfg('meijjaa')), [], 'stepper_x1: no TM'),
    'autotune on half': (lambda: awd_cfg('meijjaa') + '\n[autotune_tmc stepper_x]\nmotor: ldo\n', [],
                         'put klipper_tmc_'),
    'step distance': (lambda: re.sub(r'(\[stepper_x1\]\n(?:.*\n)*?)rotation_distance: 40',
                                     r'\1rotation_distance: 39.64', awd_cfg('meijjaa')), [],
                      'match rotation_d'),
    'microsteps': (lambda: re.sub(r'(\[stepper_x1\]\n(?:.*\n)*?)microsteps: 16', r'\1microsteps: 32',
                                  awd_cfg('meijjaa')), [], 'match microsteps'),
    'hybrid_corexy': (lambda: awd_cfg('voron-2209').replace('kinematics: corexy',
                                                           'kinematics: hybrid_corexy'), [],
                      'not on hybrid_cor'),
    'dual_carriage': (lambda: awd_cfg('meijjaa') + DUAL_CARRIAGE, [], 'not with [dual_ca'),
    'max_velocity': (lambda: awd_cfg('meijjaa').replace('max_velocity: 500', 'max_velocity: 100'),
                     ['--speed', '120'], 'raise max_veloci'),
    'travel': (lambda: awd_cfg('meijjaa'), ['--axis', 'y', '--speed', '400'], 'even a 0.40s crui'),
    'csv': (lambda: awd_cfg('meijjaa'), ['--csv'], 'drop CSV=1: runs'),
    'z': (lambda: awd_cfg('meijjaa'), [], 'raise Z to 5 mm '),
    'bed_tilt': (lambda: awd_cfg('meijjaa') + '\n[bed_tilt]\nx_adjust: 0.002\n', [],
                 'home all axes (G'),
}


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('case', REFUSALS)
def test_what_would_spoil_a_rail_run_is_refused_before_any_gcode(source, case, tmp_path):
    require(source)
    cfg, argv, shown = REFUSALS[case]
    front = klipper_front.Front(source, cfg())
    if case == 'z':
        front.run('G28 Z')                              # Z homed on the bed
    sent = len(front.scripts)
    with pytest.raises(SystemExit) as refused:
        run_tool(front, collect.collect, ['collect', '--speed', '60', '--yes', '--no-raw',
                                          '--dataset', str(tmp_path / 'g')] + argv)
    assert front.scripts[sent:] == []
    message = refused.value.code
    assert isinstance(message, str)
    display = collect.failure_display('collect FAILED: %s' % message)
    assert display.startswith('FAIL ' + shown) and display == collect.display_text(display)


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('run', list(RUNS) + ['tune'])
def test_a_rail_run_while_printing_refuses_before_it_says_to_raise_z(source, run, tmp_path,
                                                                    monkeypatch):
    # the first layers print below 5 mm: the G1 Z10 that the refusal of a low Z asks for
    # would spoil the print
    from chopper_autotune import tune
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa'))
    front.run('G28 Z')                              # Z homed on the bed
    sent = len(front.scripts)
    kl = front.connect()
    kl.is_printing = lambda: True
    with pytest.raises(collect.PrinterBusy, match='busy printing'):
        if run == 'tune':
            monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
            monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
            tune.run_tune(build_parser().parse_args(['tune', '--no-raw']))
        else:
            tool, argv = RUNS[run]
            tool(kl, build_parser().parse_args(argv + ['--dataset', str(tmp_path / 'g')]))
    assert front.scripts[sent:] == []


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('tool, argv', [
    ('resonance_map:run_resonance_map', ['map', '--axis', 'x', '--dry-run']),
    ('current:run_current_tune', ['current', '--dry-run']),
])
def test_the_one_motor_tools_refuse_a_two_motor_axis_before_any_gcode(source, tool, argv,
                                                                     monkeypatch):
    import importlib
    require(source)
    module, name = tool.split(':')
    target = importlib.import_module('chopper_autotune.' + module)
    front = klipper_front.Front(source, awd_cfg('meijjaa'))
    kl = front.connect()
    monkeypatch.setattr(target, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(target, 'find_socket', lambda explicit=None: '<front>')
    with pytest.raises(SystemExit, match=r'not on two-motor axes yet \(#129\): stepper_x1') as refused:
        getattr(target, name)(build_parser().parse_args(argv))
    assert front.scripts == []
    assert collect.failure_display('map FAILED: %s' % refused.value.code)[:16] == 'FAIL not on two-'


GRID_WINNER = tmc.Chopper(1, 4, 4, 3).fields()      # RUNS['collect']'s quieter combo (shake())


def chips(front) -> dict:
    return {section: chip.chopper() for section, chip in front.chips.items()}


def held(chip, fields: dict) -> dict:
    return {name: chip.field(name) for name in fields}


def tune_through(mk, monkeypatch, argv: 'list[str]'):
    """CHOPPER_TUNE on the printer behind `mk` (FrontMoonraker), saving through it; the
    printer it ran on."""
    from chopper_autotune import analyze, tune
    front = mk.front
    kl = front.connect()
    monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
    for module in (tune, analyze):
        monkeypatch.setattr(module, 'Moonraker', lambda url: mk)
    assert tune.run_tune(build_parser().parse_args(['tune'] + argv)) == 0
    return front


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', ['meijjaa', 'voron-2209'])
def test_tune_save_writes_each_rails_winner_into_every_section_with_one_restart(
        source, printer, monkeypatch, capsys):
    # the driver_* lines the save writes, read by the release's own TMC modules after the
    # restart: each driver of a rail holds its rail's winner
    require(source)
    mk = klipper_front.FrontMoonraker(source, awd_cfg(printer))
    resonance_at(monkeypatch)
    assert_clean(tune_through(mk, monkeypatch, ['--no-raw', '--save']))
    assert mk.restarts == 1 and mk.uploads == ['chopper-autotune/backups/printer.cfg.bak',
                                               'chopper-autotune/last-save.json', 'printer.cfg']
    winners = {}
    for root in (collect.RESULTS_HOME / 'datasets').iterdir():
        manifest = Dataset.open(root).manifest()
        winners[manifest['axis']] = manifest['winner']
    assert sorted(winners) == ['x', 'y']
    runs, summary = capsys.readouterr().out.split('=== Summary ===')
    recommended = runs.split('Recommended for printer.cfg:', 1)[1]
    for axis, winner in winners.items():
        for section in rail_sections(printer, axis):
            assert held(mk.front.chips[section], winner) == winner
            assert '[%s]' % section in recommended and '[%s]' % section in summary


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', ['meijjaa', 'voron-2209'])
@pytest.mark.parametrize('undo', ['--defaults', '--backup'])
def test_chopper_save_writes_each_rail_whole_and_restore_puts_every_driver_back_to_stock(
        source, printer, undo, tmp_path, monkeypatch):
    from chopper_autotune import analyze
    require(source)
    mk = klipper_front.FrontMoonraker(source, awd_cfg(printer))
    stock = chips(mk.front)
    roots = [tmp_path / axis for axis in 'xy']
    for axis, root in zip('xy', roots):
        assert run_tool(mk.front, collect.collect, RUNS['collect'][1] + [
            '--axis', axis, '--dataset', str(root)])[0] == 0
    assert_clean(mk.front)
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: roots)
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    assert analyze.run_save_latest(build_parser().parse_args(['save'])) == 0
    assert mk.restarts == 1 and mk.uploads == ['chopper-autotune/backups/printer.cfg.bak',
                                               'chopper-autotune/last-save.json', 'printer.cfg']
    for axis in 'xy':
        for section in rail_sections(printer, axis):
            assert held(mk.front.chips[section], GRID_WINNER) == GRID_WINNER
    # before the save every driver ran the Klipper defaults: either undo brings them back
    assert analyze.run_restore_config(build_parser().parse_args(['restore', undo])) == 0
    assert mk.restarts == 2 and chips(mk.front) == stock


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', AWD)
def test_analyze_applies_and_saves_a_rails_winner_to_each_of_its_drivers(source, printer, tmp_path,
                                                                        monkeypatch, capsys):
    from chopper_autotune import analyze
    require(source)
    mk = klipper_front.FrontMoonraker(source, awd_cfg(printer))
    stock = chips(mk.front)
    root = tmp_path / 'x'
    assert run_tool(mk.front, collect.collect, RUNS['collect'][1] + [
        '--axis', 'x', '--dataset', str(root)])[0] == 0
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    capsys.readouterr()
    assert analyze.run_analyze(build_parser().parse_args(['analyze', str(root), '--no-html',
                                                          '--apply'])) == 0
    recommended = capsys.readouterr().out.split('Recommended for printer.cfg:')[1]
    for section in rail_sections(printer, 'x'):
        assert '[%s]' % section in recommended
    assert_clean(mk.front)

    def motor_a_on_the_winner():
        for section in rail_sections(printer, 'x'):
            assert held(mk.front.chips[section], GRID_WINNER) == GRID_WINNER
        for section in rail_sections(printer, 'y'):
            assert mk.front.chips[section].chopper() == stock[section]
    assert mk.restarts == 0
    motor_a_on_the_winner()
    front = mk.front                    # the save restarts Klipper: a new console
    assert analyze.run_analyze(build_parser().parse_args(['analyze', str(root), '--no-html',
                                                          '--save'])) == 0
    assert mk.restarts == 1
    motor_a_on_the_winner()
    sections = ' and '.join('[%s]' % section for section in rail_sections(printer, 'x'))
    assert [line for line in front.console if line.startswith('Chopper: motor A: ')] == [
        'Chopper: motor A: saving %s into %s from run x' % (tmc.Chopper(**GRID_WINNER).label(),
                                                            sections)]


@pytest.mark.parametrize('source', SOURCES)
def test_a_dataset_of_one_motor_is_not_saved_on_an_axis_that_has_a_twin_now(source, tmp_path,
                                                                            monkeypatch, capsys):
    # tuned before stepper_x1 came: its winner would reach one driver of the pair. ANALYZE
    # refuses it; CHOPPER_SAVE skips motor A whole and still saves motor B's rail
    from chopper_autotune import analyze
    require(source)
    single, pair = tmp_path / 'single', tmp_path / 'pair'
    assert run_tool(klipper_front.Front(source, awd_cfg('meijjaa', twins=False)), collect.collect,
                    RUNS['collect'][1] + ['--axis', 'x', '--dataset', str(single)])[0] == 0
    mk = klipper_front.FrontMoonraker(source, awd_cfg('meijjaa'))
    stock = chips(mk.front)
    assert run_tool(mk.front, collect.collect, RUNS['collect'][1] + [
        '--axis', 'y', '--dataset', str(pair)])[0] == 0
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    for flag in ('--apply', '--save'):
        with pytest.raises(SystemExit, match='tune motor A again: its dataset measured stepper_x alone'):
            analyze.run_analyze(build_parser().parse_args(['analyze', str(single), '--no-html', flag]))
    assert mk.uploads == [] and mk.restarts == 0 and chips(mk.front) == stock
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: [single, pair])
    assert analyze.run_save_latest(build_parser().parse_args(['save'])) == 0
    assert 'motor A: tune motor A again: its dataset measured stepper_x alone' \
        in capsys.readouterr().out
    assert mk.restarts == 1
    for section in rail_sections('meijjaa', 'x'):
        assert mk.front.chips[section].chopper() == stock[section]
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], GRID_WINNER) == GRID_WINNER


@pytest.mark.parametrize('source', SOURCES)
def test_a_rail_whose_twin_runs_another_driver_now_is_skipped_and_the_rest_saved(
        source, tmp_path, monkeypatch, capsys):
    # stepper_x1 got a TMC2209 after the run: no section of the model the rail measured.
    # CHOPPER_SAVE skips motor A whole and still saves motor B; ANALYZE refuses before a write
    from chopper_autotune import analyze
    require(source)
    mk = klipper_front.FrontMoonraker(source, awd_cfg('meijjaa'))
    roots = [tmp_path / axis for axis in 'xy']
    for axis, root in zip('xy', roots):
        assert run_tool(mk.front, collect.collect, RUNS['collect'][1] + [
            '--axis', axis, '--dataset', str(root)])[0] == 0
    mk.files['printer.cfg'] = mismatched_model(mk.files['printer.cfg'])
    mk.gcode('RESTART')
    stock = chips(mk.front)
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    refusal = ('tune motor A again: its dataset measured TMC5160 drivers, the config gives '
               'stepper_x1 TMC2209 now')
    for flag in ('--apply', '--save'):
        with pytest.raises(SystemExit, match=re.escape(refusal)):
            analyze.run_analyze(build_parser().parse_args(['analyze', str(roots[0]), '--no-html',
                                                           flag]))
    assert mk.uploads == [] and mk.restarts == 1 and chips(mk.front) == stock
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: roots)
    assert analyze.run_save_latest(build_parser().parse_args(['save'])) == 0
    assert 'motor A: ' + refusal in capsys.readouterr().out
    assert mk.restarts == 2
    for section in ('tmc5160 stepper_x', 'tmc2209 stepper_x1'):
        assert mk.front.chips[section].chopper() == stock[section]
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], GRID_WINNER) == GRID_WINNER


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('failing', [1, 2])
def test_an_apply_the_twin_fails_puts_every_driver_of_the_rail_back(source, failing, tmp_path,
                                                                     monkeypatch):
    # Klipper stops the script at the line that fails, after the main motor took the set:
    # each driver gets the registers of its config back, or, when that fails too, a restart
    from chopper_autotune import analyze
    require(source)
    mk = klipper_front.FrontMoonraker(source, own_cfg('meijjaa'))
    root = tmp_path / 'x'
    assert run_tool(mk.front, collect.collect, RUNS['collect'][1] + [
        '--axis', 'x', '--dataset', str(root)])[0] == 0
    configured = chips(mk.front)
    twin = mk.front.chips[rail_sections('meijjaa', 'x')[1]]
    write, refused = twin.set_register, []

    def set_register(reg_name, val, print_time=None):
        if reg_name == 'CHOPCONF' and len(refused) < failing:
            refused.append(val)
            raise mk.front.printer.command_error("Unable to write tmc spi 'stepper_x1' register "
                                                 'CHOPCONF')
        write(reg_name, val, print_time)
    twin.set_register = set_register
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    with pytest.raises(SystemExit) as stopped:
        analyze.run_analyze(build_parser().parse_args(['analyze', str(root), '--no-html',
                                                       '--apply']))
    shown = collect.failure_display('analyze FAILED: %s' % stopped.value.code)
    if failing == 1:
        assert shown.startswith('FAIL check the drivers of motor A (DUMP_TMC): setting ')
        assert chips(mk.front) == configured
    else:
        assert shown.startswith('FAIL restart Klipper (RESTART): setting ')
        assert 'stepper_x1 could not be put back' in stopped.value.code
        assert chips(mk.front)[rail_sections('meijjaa', 'x')[0]] \
            == configured[rail_sections('meijjaa', 'x')[0]]


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('managed, refusal', [
    (('stepper_x', 'stepper_x1', 'stepper_y', 'stepper_y1'),
     'not saving [tmc2209 stepper_x] and [tmc2209 stepper_x1]: autotune resets'),
    # motor A is free: the tune of both would run it, then refuse B's save
    (('stepper_y', 'stepper_y1'), 'not saving [tmc2209 stepper_y] and [tmc2209 stepper_y1]: '),
    (('stepper_y1',), 'put klipper_tmc_autotune on all of stepper_y, stepper_y1 or on none'),
])
def test_tune_save_refuses_a_rail_autotune_manages_before_anything_moves(source, managed, refusal,
                                                                       monkeypatch):
    # klipper_tmc_autotune writes its own chopper at every start: say it now, naming every
    # section of the rail, not after the tuning
    require(source)
    mk = klipper_front.FrontMoonraker(source, awd_cfg('voron-2209') + ''.join(
        '\n[autotune_tmc %s]\nmotor: ldo-42sth48-2004ac\n' % name for name in managed))
    with pytest.raises(SystemExit, match=re.escape(refusal)):
        tune_through(mk, monkeypatch, ['--save'])
    assert mk.front.scripts == [] and mk.uploads == []


@pytest.mark.parametrize('source', SOURCES)
def test_a_tune_of_a_rail_autotune_manages_names_each_section_and_its_threshold(source, monkeypatch,
                                                                               capsys):
    # the summary alone says how to switch autotune off, once, for each driver: a section
    # left on one of them refuses the next rail run
    require(source)
    mk = klipper_front.FrontMoonraker(source, awd_cfg('voron-2209') + ''.join(
        '\n[autotune_tmc %s]\nmotor: ldo-42sth48-2004ac\nsg4_thrs: %d\n' % pair
        for pair in (('stepper_x', 80), ('stepper_x1', 90))))
    resonance_at(monkeypatch)
    assert_clean(tune_through(mk, monkeypatch, ['--motor', 'a', '--no-raw']))
    out = capsys.readouterr().out
    summary = out.split('=== Summary ===')[1]
    advice = ('klipper_tmc_autotune ([autotune_tmc stepper_x] and [autotune_tmc stepper_x1]) '
              'writes its own')
    assert 'stepper_x, stepper_x1: ' + advice in summary and out.count(advice) == 1
    assert 'in [tmc2209 stepper_x] set driver_SGTHRS: 80' in summary
    assert 'in [tmc2209 stepper_x1] set driver_SGTHRS: 90' in summary


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('run', RUNS)
def test_a_dataset_of_one_motor_does_not_resume_as_a_rail(source, run, tmp_path):
    # its FORCE_MOVEs of stepper_x and the rail's G1s would mix under one combo or speed
    require(source)
    tool, argv = RUNS[run]
    root = tmp_path / 'dataset'
    argv = argv + ['--axis', 'x', '--dataset', str(root)]
    assert run_tool(klipper_front.Front(source, awd_cfg('meijjaa', twins=False)), tool, argv)[0] == 0
    records = Dataset.open(root).records()
    front = klipper_front.Front(source, awd_cfg('meijjaa'))
    with pytest.raises(SystemExit, match=re.escape(
            'start a new dataset (no DATASET=): this one moved stepper_x alone by FORCE_MOVE, this '
            'run moves stepper_x, stepper_x1 together by G1')):
        run_tool(front, tool, argv)
    assert front.head_moves == [] and front.homings == []
    assert Dataset.open(root).records() == records


@pytest.mark.parametrize('source', SOURCES)
def test_spreadcycle_forced_on_the_twin_alone_is_recorded(source, tmp_path):
    require(source)
    front = klipper_front.Front(source, awd_cfg('voron-2209').replace(
        '[tmc2209 stepper_x1]\n', '[tmc2209 stepper_x1]\nstealthchop_threshold: 999999\n'))
    root = tmp_path / 'grid'
    assert run_tool(front, collect.collect, RUNS['collect'][1] + [
        '--axis', 'x', '--dataset', str(root)])[0] == 0
    assert Dataset.open(root).manifest()['forced_spreadcycle'] is True


EDGES = 'driver_TBL: 0\ndriver_TOFF: 3\ndriver_HSTRT: 3\ndriver_HEND: 15\n'   # both edges at once


@pytest.mark.parametrize('source', SOURCES)
def test_a_rail_on_saved_edges_starts_inside_the_ranges_and_keeps_them_out_of_the_rounds(
        source, tmp_path, capsys):
    # both drivers of the pair hold TBL 0 and hysteresis 16: the descent would start on the
    # edges, and the back-to-back rounds keep them as the rail's own registers
    require(source)
    cfg = awd_cfg('voron-2209')
    for section in rail_sections('voron-2209', 'x'):
        cfg = cfg.replace('[%s]\n' % section, '[%s]\n%s' % (section, EDGES))
    front = klipper_front.Front(source, cfg)
    root = tmp_path / 'descent'
    assert run_tool(front, collect.collect, [
        'collect', '--axis', 'x', '--speed', '60', '--search', 'descent', '--toff', '3:4',
        '--hstrt', '2:3', '--hend', '14:15', '--yes', '--no-raw', '--dataset', str(root)])[0] == 0
    assert_clean(front)
    moves = [tmc.Chopper(record['tbl'], record['toff'], record['hstrt'], record['hend'])
             for record in Dataset.open(root).records() if record.get('kind') == 'move']
    # the nearest combo inside: TBL 1, and HSTRT one lower to keep HEND 15 at hysteresis 15
    assert moves[0] == tmc.Chopper(1, 3, 2, 15)
    assert not [combo for combo in moves if combo.tbl == 0 or tmc.effective_hysteresis(combo) > 15]
    assert ('Your registers tbl0_toff3_hstrt3_hend15 are not measured back to back (TBL 0 is '
            'outside TBL=1:3 and hysteresis 16 is above MAX_HYSTERESIS=15): the winner is not '
            'checked against them') in capsys.readouterr().out.splitlines()
    assert not Dataset.open(root).manifest()['kept']


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('autotune', [False, True])
def test_a_rail_run_on_its_own_names_the_next_command_for_the_pair(source, autotune, monkeypatch,
                                                                    tmp_path, capsys):
    # the scan names the tune of the rail at its resonance; the register search how to hear
    # and save the pair, or, under klipper_tmc_autotune, what that means for each driver
    from chopper_autotune import find_speed
    require(source)
    front = klipper_front.Front(source, awd_cfg('meijjaa') + ''.join(
        '\n[autotune_tmc %s]\nmotor: ldo-42sth48-2004ac\n' % name
        for name in ('stepper_x', 'stepper_x1') if autotune))
    for module in (find_speed, collect):
        monkeypatch.setattr(module, 'Klippy', lambda path: types.SimpleNamespace(connect=front.connect))
        monkeypatch.setattr(module, 'find_socket', lambda explicit=None: '<front>')
    assert find_speed.run_find_speed(build_parser().parse_args([
        'find-speed', '--axis', 'x', '--min-speed', '30', '--max-speed', '90', '--step', '10',
        '--yes', '--no-raw', '--dataset', str(tmp_path / 'scan')])) == 0
    assert collect.run_collect(build_parser().parse_args(RUNS['collect'][1] + [
        '--axis', 'x', '--dataset', str(tmp_path / 'x')])) == 0
    assert_clean(front)
    told = [line for line in front.console if line.startswith('Chopper: To ')]
    assert told[0] == 'Chopper: To tune motor A at its resonance speed: CHOPPER_TUNE MOTOR=A SPEED=60'
    out = capsys.readouterr().out
    if autotune:
        assert told[1:] == []
        advice = ('klipper_tmc_autotune ([autotune_tmc stepper_x] and [autotune_tmc stepper_x1]) '
                  'writes its own')
        assert 'stepper_x, stepper_x1: ' + advice in out and out.count(advice) == 1
    else:
        # without MOTOR= ANALYZE would take the newest run of any motor
        assert told[1:] == [
            'Chopper: To hear the registers this run found, set them live: '
            'CHOPPER_ANALYZE MOTOR=A APPLY=1',
            'Chopper: To save the registers this run found: CHOPPER_SAVE MOTOR=A']
        assert pasted(told[1]).axis == 'x' and pasted(told[1]).apply
        assert 'klipper_tmc_autotune' not in out


def collect_motor(mk, axis: str, argv: 'list[str] | None' = None) -> str:
    """CHOPPER_COLLECT MOTOR=<axis> into the dataset folder of the runs, as the macro runs it."""
    code, root = run_tool(mk.front, collect.collect, (argv or RUNS['collect'][1]) + ['--axis', axis])
    assert code == 0
    return root


def analyze_through(mk, monkeypatch, line: str) -> 'list[str]':
    """The CHOPPER_ANALYZE at the end of `line`, pasted into the console of the printer
    behind `mk`, without the report: the console lines it added."""
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    # not NO_HTML=1: the save command of APPLY repeats what was pasted
    monkeypatch.setattr(analyze, 'write_report', lambda *args, **kwargs: None)
    args = pasted(line)
    args.dataset = args.dataset_opt
    before = len(mk.front.console)
    assert analyze.run_analyze(args) == 0
    return mk.front.console[before:]


def stopped_run(mk, monkeypatch, axis: str):
    """A COLLECT stopped before its first measurement (CHOPPER_STOP at once): the newest
    dataset, without one. Its own folder: a run of the same second would resume the last."""
    def stop(*args):
        raise SystemExit(143)
    with monkeypatch.context() as patch:
        patch.setattr(collect, 'measure_baseline', stop)
        with pytest.raises(SystemExit) as stopped:
            run_tool(mk.front, collect.collect, RUNS['collect'][1] + [
                '--axis', axis, '--dataset', str(collect.RESULTS_HOME / 'datasets' / 'stopped')])
        assert stopped.value.code == 143


@pytest.mark.parametrize('source', SOURCES)
def test_apply_on_a_rail_tells_the_console_what_it_set_where_and_how_to_keep_it(source,
                                                                                monkeypatch):
    # APPLY runs detached: its log is unseen, and Klipper prints nothing for SET_TMC_FIELD
    require(source)
    mk = klipper_front.FrontMoonraker(source, awd_cfg('meijjaa'))
    stock = chips(mk.front)
    root = collect_motor(mk, 'y')
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1')
    assert told == [
        'Chopper: Motor B: tbl1_toff4_hstrt4_hend3 from run %s is set on stepper_y and stepper_y1 '
        'until Klipper restarts. CHOPPER_DEMO refuses to start while stepper_y and stepper_y1 run '
        'them. At its end, the demo would put back the registers Klipper loaded from its config. '
        'After you save them, CHOPPER_DEMO plays them. '
        'To save them: CHOPPER_ANALYZE DATASET=%s SAVE=1' % (root.rsplit('/', 1)[-1], root)]
    assert_clean(mk.front)
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], GRID_WINNER) == GRID_WINNER
    for section in rail_sections('meijjaa', 'x'):
        assert mk.front.chips[section].chopper() == stock[section]
    # the command at its end saves what was set
    analyze_through(mk, monkeypatch, told[0])
    assert mk.restarts == 1
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], GRID_WINNER) == GRID_WINNER


@pytest.mark.parametrize('source', SOURCES)
def test_analyze_of_a_motor_takes_its_newest_run_that_measured(source, monkeypatch, capsys):
    # motor A ran after B, and a run of B stopped at once: MOTOR=B takes the B run that measured
    require(source)
    mk = klipper_front.FrontMoonraker(source, awd_cfg('meijjaa'))
    stock = chips(mk.front)
    with pytest.raises(SystemExit,
                       match='^tune motor B first: none of its tuning runs has measurements$'):
        analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1')
    root = collect_motor(mk, 'y')
    other = collect_motor(mk, 'x')
    stopped_run(mk, monkeypatch, 'y')
    capsys.readouterr()
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1')
    assert told[0].startswith('Chopper: Motor B: tbl1_toff4_hstrt4_hend3 from run %s '
                              % root.rsplit('/', 1)[-1])
    assert 'motor B: skipping run stopped: it has no measurements' in capsys.readouterr().out
    # without MOTOR= the newest run of any motor: the stopped one
    with pytest.raises(SystemExit, match='^add MOTOR=A or MOTOR=B: the newest run, stopped, has no '
                                         'successful measurements. With MOTOR=, CHOPPER_ANALYZE '
                                         'takes the newest run of that motor that has them$'):
        analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE APPLY=1')
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], GRID_WINNER) == GRID_WINNER
    for section in rail_sections('meijjaa', 'x'):
        assert mk.front.chips[section].chopper() == stock[section]
    with pytest.raises(SystemExit, match='^drop DATASET= or MOTOR=: %s is a run of motor A, not '
                                         'of motor B$' % other):
        analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE DATASET=%s MOTOR=B APPLY=1' % other)
    assert collect.failure_display('analyze FAILED: tune motor B first')[:16] == 'FAIL tune motor '


def noisy_head(chopper_weight: float, jitter: float, best: dict):
    """Front.head_vibration with a weak chopper effect, quietest at `best`, and a jitter
    of each move: a motor whose registers barely matter, measured with noise, as meijjaa's
    motor B (1.1x)."""
    def head_vibration(self, move):
        mg = 0.
        for axis in 'xy':
            motors = self.rail(axis)
            if move['belts'][axis] and motors:
                for motor in motors:
                    chop = self.chopper(move, motor)
                    base = 40. + 600. * math.exp(-((move['belts'][axis] - 60.) / 10.) ** 2)
                    mg += base * (1. + chopper_weight * sum(
                        abs(chop.get(name, value) - value) for name, value in best.items())) \
                        / len(motors)
        if not mg:
            return klipper_front.STANDSTILL_MG
        return mg * (1. + jitter * math.sin(move['window'][0] * 7919.))
    return head_vibration


AROUND_STOCK = ['collect', '--speed', '60', '--tbl', '2:2', '--toff', '3:4', '--hstrt', '5:5',
                '--hend', '2:3', '--iterations', '1', '--yes', '--no-raw']
STOCK_5160 = {'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 2}


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('weight, jitter, best, kept', [
    (0.03, 0.08, {'toff': 4, 'hend': 3}, True), (0.1, 0., {'toff': 3, 'hend': 2}, False)],
    ids=['kept', 'best-is-the-config'])
def test_a_rail_run_ending_on_the_registers_of_the_config_offers_nothing_to_hear_or_save(
        source, weight, jitter, best, kept, monkeypatch):
    # motor B on Klipper defaults (CHOPPER_RESTORE DEFAULTS=1), registers that barely matter:
    # the run ends on the defaults, kept or found the best
    require(source)
    monkeypatch.setattr(klipper_front.Front, 'head_vibration', noisy_head(weight, jitter, best))
    mk = klipper_front.FrontMoonraker(source, awd_cfg('meijjaa'))
    stock = chips(mk.front)
    root = collect_motor(mk, 'y', AROUND_STOCK)
    manifest = Dataset.open(root).manifest()
    assert (manifest['winner'], manifest['kept']) == (STOCK_5160, kept)
    before = len(mk.front.console)
    collect.what_next(mk.front.connect(), Dataset.open(root), mk)
    assert not [line for line in mk.front.console[before:] if 'CHOPPER_' in line]
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    assert analyze.run_save_latest(build_parser().parse_args(['save', '--motor', 'b'])) == 0
    assert mk.restarts == 0
    name = root.rsplit('/', 1)[-1]
    said = [line for line in mk.front.console if line.startswith('Chopper: motor B: ')]
    sections = '[tmc5160 stepper_y] and [tmc5160 stepper_y1]'
    assert said == ['Chopper: motor B: your registers tbl2_toff3_hstrt5_hend2 stay in %s. In run '
                    '%s, neither the winner nor Klipper defaults was quieter than them in all 4 '
                    'rounds' % (sections, name) if kept else
                    'Chopper: motor B: the config already has tbl2_toff3_hstrt5_hend2, the result '
                    'of run %s, in %s, so there is nothing to write' % (name, sections)]
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1')
    assert told == ['Chopper: Motor B: tbl2_toff3_hstrt5_hend2 from run %s is set on stepper_y and '
                    'stepper_y1. These are the registers Klipper loaded from its config at its '
                    'last start. The run found nothing to change' % name]
    assert chips(mk.front) == stock


@pytest.mark.parametrize('source', SOURCES)
def test_a_rail_run_ending_on_registers_a_restart_would_change_names_how_to_save_them(
        source, monkeypatch):
    # printer.cfg gives stepper_y1 a set of its own since Klipper started, as a copy moved
    # without a restart leaves it: the drivers run the defaults, the run ends on them
    require(source)
    monkeypatch.setattr(klipper_front.Front, 'head_vibration', noisy_head(0.1, 0., STOCK_5160))
    mk = klipper_front.FrontMoonraker(source, awd_cfg('meijjaa'))
    stock = chips(mk.front)
    mk.files['printer.cfg'] = mk.files['printer.cfg'].replace(
        '[tmc5160 stepper_y1]\n', '[tmc5160 stepper_y1]\ndriver_TOFF: 4\ndriver_HEND: 3\n')
    root = collect_motor(mk, 'y', AROUND_STOCK)
    assert Dataset.open(root).manifest()['winner'] == STOCK_5160
    before = len(mk.front.console)
    collect.what_next(mk.front.connect(), Dataset.open(root), mk)
    assert mk.front.console[before:] == [
        'Chopper: To hear the registers this run found, set them live: '
        'CHOPPER_ANALYZE MOTOR=B APPLY=1',
        'Chopper: To save the registers this run found: CHOPPER_SAVE MOTOR=B']
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1')
    assert told[0].endswith('until Klipper restarts. Klipper runs them from the config it loaded. '
                            'At its next start, Klipper loads other registers from its config '
                            'files. To save them: CHOPPER_ANALYZE DATASET=%s SAVE=1' % root)
    front = tune_through(mk, monkeypatch, AROUND_STOCK[1:-2] + ['--motor', 'b', '--tpfd', '4',
                                                                '--no-raw'])
    done = [line for line in front.console if line.startswith('echo: Tune done: ')]
    assert len(done) == 1 and done[0].endswith(' — to save: CHOPPER_SAVE MOTOR=B')
    assert mk.restarts == 0
    assert analyze.run_save_latest(build_parser().parse_args(['save', '--motor', 'b'])) == 0
    assert mk.restarts == 1 and chips(mk.front) == stock


@pytest.mark.parametrize('source', SOURCES)
def test_a_rail_run_ending_on_the_registers_of_one_driver_names_how_to_hear_and_save_them(
        source, monkeypatch):
    # stepper_y on Klipper defaults, stepper_y1 on a set of its own: the run ends on the
    # defaults, which only stepper_y gets from the config
    require(source)
    monkeypatch.setattr(klipper_front.Front, 'head_vibration', noisy_head(0.1, 0., STOCK_5160))
    cfg = awd_cfg('meijjaa').replace('[tmc5160 stepper_y1]\n',
                                     '[tmc5160 stepper_y1]\ndriver_TOFF: 4\ndriver_HEND: 3\n')
    mk = klipper_front.FrontMoonraker(source, cfg)
    root = collect_motor(mk, 'y', AROUND_STOCK)
    assert Dataset.open(root).manifest()['winner'] == STOCK_5160
    before = len(mk.front.console)
    collect.what_next(mk.front.connect(), Dataset.open(root), mk)
    assert mk.front.console[before:] == [
        'Chopper: To hear the registers this run found, set them live: '
        'CHOPPER_ANALYZE MOTOR=B APPLY=1',
        'Chopper: To save the registers this run found: CHOPPER_SAVE MOTOR=B']
    name = root.rsplit('/', 1)[-1]
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    assert analyze.run_save_latest(build_parser().parse_args(['save', '--motor', 'b',
                                                              '--dry-run'])) == 0
    assert [line for line in mk.front.console if line.startswith('Chopper: motor B: ')] == [
        'Chopper: motor B: would save tbl2_toff3_hstrt5_hend2 into [tmc5160 stepper_y] and '
        '[tmc5160 stepper_y1] from run %s' % name]
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1')
    assert told == ['Chopper: Motor B: tbl2_toff3_hstrt5_hend2 from run %s is set on stepper_y and '
                    'stepper_y1 until Klipper restarts. These are the Klipper defaults. '
                    'CHOPPER_DEMO refuses to start while stepper_y1 runs them. After you save '
                    'them, it has nothing to play against the defaults. To save them: '
                    'CHOPPER_ANALYZE DATASET=%s SAVE=1' % (name, root)]
    analyze_through(mk, monkeypatch, told[0])
    assert mk.restarts == 1
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], STOCK_5160) == STOCK_5160


@pytest.mark.parametrize('source', SOURCES)
def test_the_save_command_of_apply_on_a_rail_saves_the_set_its_hearing_chose(source, monkeypatch):
    # AUDIBLE_KHZ=36 SKIP_AUDIBLE=1 leaves out the winner the run recorded (34 kHz)
    require(source)
    mk = klipper_front.FrontMoonraker(source, awd_cfg('meijjaa'))
    root = collect_motor(mk, 'y')
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1 AUDIBLE_KHZ=36 '
                                            'SKIP_AUDIBLE=1')
    assert told[0].endswith('To save them: CHOPPER_ANALYZE DATASET=%s AUDIBLE_KHZ=36 '
                            'SKIP_AUDIBLE=1 SAVE=1' % root)
    heard = tmc.Chopper(1, 3, 4, 3).fields()
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], heard) == heard
    analyze_through(mk, monkeypatch, told[0])
    assert mk.restarts == 1
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], heard) == heard


@pytest.mark.parametrize('source', SOURCES)
def test_apply_on_a_rail_klipper_tmc_autotune_manages_names_no_config_and_no_save(source,
                                                                                 monkeypatch):
    # autotune writes its own registers at every start: the driver_* lines, here the very
    # winner, say nothing of what a restart brings, and a save would not reach the drivers
    require(source)
    lines = ''.join('driver_%s: %d\n' % (name.upper(), value) for name, value in GRID_WINNER.items())
    cfg = awd_cfg('meijjaa') + ''.join('\n[autotune_tmc %s]\nmotor: ldo-42sth48-2004ac\n' % name
                                       for name in ('stepper_y', 'stepper_y1'))
    for section in rail_sections('meijjaa', 'y'):
        cfg = cfg.replace('[%s]\n' % section, '[%s]\n%s' % (section, lines))
    mk = klipper_front.FrontMoonraker(source, cfg)
    root = collect_motor(mk, 'y')
    assert analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1') == [
        'Chopper: Motor B: tbl1_toff4_hstrt4_hend3 from run %s is set on stepper_y and stepper_y1 '
        'until Klipper restarts' % root.rsplit('/', 1)[-1]]


@pytest.mark.parametrize('source', SOURCES)
def test_apply_of_the_klipper_defaults_says_the_demo_has_nothing_to_play_with_them(source,
                                                                                  monkeypatch):
    # motor B on a noisy set of its own, and the defaults beat it
    require(source)
    monkeypatch.setattr(klipper_front.Front, 'head_vibration', noisy_head(0.1, 0., STOCK_5160))
    noisy = 'driver_TBL: 0\ndriver_TOFF: 2\ndriver_HSTRT: 3\ndriver_HEND: 15\n'
    cfg = awd_cfg('meijjaa')
    for section in rail_sections('meijjaa', 'y'):
        cfg = cfg.replace('[%s]\n' % section, '[%s]\n%s' % (section, noisy))
    mk = klipper_front.FrontMoonraker(source, cfg)
    root = collect_motor(mk, 'y', AROUND_STOCK)
    assert Dataset.open(root).manifest()['winner'] == STOCK_5160
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1')
    assert told == ['Chopper: Motor B: tbl2_toff3_hstrt5_hend2 from run %s is set on stepper_y and '
                    'stepper_y1 until Klipper restarts. These are the Klipper defaults. '
                    'CHOPPER_DEMO refuses to start while stepper_y and stepper_y1 run them. After '
                    'you save them, it has nothing to play against the defaults. To save them: '
                    'CHOPPER_ANALYZE DATASET=%s SAVE=1' % (root.rsplit('/', 1)[-1], root)]
    for section in rail_sections('meijjaa', 'y'):
        assert held(mk.front.chips[section], STOCK_5160) == STOCK_5160
