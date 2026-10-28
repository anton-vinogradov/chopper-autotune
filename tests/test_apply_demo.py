"""One motor per axis on printers built from each release's own modules
(tests/klipper_front.py): CHOPPER_ANALYZE APPLY=1 says on the console what it set and how
to keep it, CHOPPER_ANALYZE MOTOR= takes that motor's run, a run ending on the registers
Klipper runs and reads again names nothing to hear or save, and CHOPPER_DEMO refuses
drivers set live before anything moves instead of undoing them; saves through
FrontMoonraker."""
import json
import math
import re
import types
from dataclasses import replace

import pytest

import klipper_front
from chopper_autotune import analyze, collect, demo, find_speed, tmc
from chopper_autotune.analyze import register_options, updated_options
from chopper_autotune.cli import build_parser
from chopper_autotune.dataset import Dataset
from test_klipper_front import SOURCES, assert_clean, printer_cfg, require, run_tool
from test_next_step import pasted

GRID = ['collect', '--speed', '60', '--tbl', '1:1', '--toff', '3:4', '--hstrt', '4:4', '--hend',
        '3:3', '--iterations', '1', '--yes', '--no-raw']
GRID_WINNER = tmc.Chopper(1, 4, 4, 3)           # GRID's quieter combo (Front.vibration)
STOCK = tmc.DRIVERS['2209'].default
AROUND_STOCK = ['collect', '--speed', '60', '--tbl', '2:2', '--toff', '3:4', '--hstrt', '5:5',
                '--hend', '0:1', '--iterations', '1', '--yes', '--no-raw']


@pytest.fixture(autouse=True)
def printer_time(monkeypatch):
    monkeypatch.setattr(collect, 'time', klipper_front.Clock())


def chips(front) -> dict:
    return {section: chip.chopper() for section, chip in front.chips.items()}


def held(chip, combo: tmc.Chopper) -> tmc.Chopper:
    """The registers of combo as the chip holds them."""
    return tmc.Chopper(**{name: chip.field(name) for name in combo.fields()})


def tuned(cfg: str, driver: str = '2209', **sections: tmc.Chopper) -> str:
    """cfg with these registers in [tmc<driver> <stepper>] (stepper_x=...)."""
    for stepper, combo in sections.items():
        cfg = updated_options(cfg, 'tmc%s %s' % (driver, stepper), register_options(combo.fields()))
    return cfg


def collect_motor(mk, axis: str, argv: 'list[str]' = GRID) -> str:
    """CHOPPER_COLLECT MOTOR=<axis> into the dataset folder of the runs, as the macro runs it."""
    code, root = run_tool(mk.front, collect.collect, argv + ['--axis', axis])
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


def save_through(mk, monkeypatch, *argv: str) -> 'list[str]':
    """CHOPPER_SAVE through `mk`: the console lines of the motors."""
    front = mk.front                    # a save restarts Klipper: a new console
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    assert analyze.run_save_latest(build_parser().parse_args(['save', *argv])) == 0
    return [line for line in front.console if line.startswith('Chopper: motor ')]


def stopped_run(mk, monkeypatch, axis: str):
    """A COLLECT stopped before its first measurement (CHOPPER_STOP at once): the newest
    dataset, without one. Its own folder: a run of the same second would resume the last."""
    def stop(*args):
        raise SystemExit(143)
    with monkeypatch.context() as patch:
        patch.setattr(collect, 'measure_baseline', stop)
        with pytest.raises(SystemExit) as stopped:
            run_tool(mk.front, collect.collect, GRID + [
                '--axis', axis, '--dataset', str(collect.RESULTS_HOME / 'datasets' / 'stopped')])
        assert stopped.value.code == 143


def hints(mk, root) -> 'list[str]':
    """What CHOPPER_COLLECT names at the end of the run in `root`, on the console."""
    before = len(mk.front.console)
    kl = mk.front.connect()
    try:
        collect.what_next(kl, Dataset.open(root), mk)
    finally:
        kl.close()
    return mk.front.console[before:]


def quiet_at(best: dict, weight: float = 0.1, jitter: float = 0.):
    """Front.vibration with a chopper effect of `weight`, quietest at `best`, and a jitter
    of each move: a motor whose registers barely matter, measured with noise."""
    def vibration(move):
        chopper = move['chips'].get(next((section for section in move['chips']
                                          if section.endswith(' ' + move['stepper'])), ''), {})
        base = 40. + 600. * math.exp(-((move['speed'] - 60.) / 10.) ** 2)
        return base * (1. + weight * sum(abs(chopper.get(name, value) - value)
                                         for name, value in best.items())) \
            * (1. + jitter * math.sin(move['window'][0] * 7919.))
    return staticmethod(vibration)


ON_STOCK = {'kept': (0.03, 0.08, {'toff': 4, 'hend': 1}, True),
            'best is the config': (0.1, 0., {'toff': 3, 'hend': 0}, False)}


@pytest.mark.parametrize('source', SOURCES)
def test_apply_tells_the_console_what_it_set_where_and_how_to_keep_it(source, monkeypatch):
    # APPLY runs detached: its log is unseen, and Klipper prints nothing for SET_TMC_FIELD
    require(source)
    mk = klipper_front.FrontMoonraker(source, printer_cfg())
    stock = chips(mk.front)
    root = collect_motor(mk, 'x')
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=A APPLY=1')
    assert told == [
        'Chopper: Motor A: %s from run %s is set on stepper_x until Klipper restarts. '
        'CHOPPER_DEMO refuses to start while stepper_x runs them. At its end, the demo would put '
        'back the registers Klipper loaded from its config. After you save them, CHOPPER_DEMO '
        'plays them. To save them: CHOPPER_ANALYZE DATASET=%s SAVE=1'
        % (GRID_WINNER.label(), root.rsplit('/', 1)[-1], root)]
    assert_clean(mk.front)
    assert held(mk.front.chips['tmc2209 stepper_x'], GRID_WINNER) == GRID_WINNER
    assert chips(mk.front)['tmc2209 stepper_y'] == stock['tmc2209 stepper_y']
    # the command at its end saves what was set
    analyze_through(mk, monkeypatch, told[0])
    assert mk.restarts == 1
    assert held(mk.front.chips['tmc2209 stepper_x'], GRID_WINNER) == GRID_WINNER


@pytest.mark.parametrize('source', SOURCES)
def test_the_save_command_of_apply_saves_the_set_its_hearing_chose(source, monkeypatch):
    # AUDIBLE_KHZ=36 SKIP_AUDIBLE=1 leaves out the winner the run recorded (34 kHz)
    require(source)
    mk = klipper_front.FrontMoonraker(source, printer_cfg())
    root = collect_motor(mk, 'x')
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=A APPLY=1 AUDIBLE_KHZ=36 '
                                            'SKIP_AUDIBLE=1')
    assert told[0].endswith('To save them: CHOPPER_ANALYZE DATASET=%s AUDIBLE_KHZ=36 '
                            'SKIP_AUDIBLE=1 SAVE=1' % root)
    heard = tmc.Chopper(1, 3, 4, 3)
    assert held(mk.front.chips['tmc2209 stepper_x'], heard) == heard
    analyze_through(mk, monkeypatch, told[0])
    assert mk.restarts == 1
    assert held(mk.front.chips['tmc2209 stepper_x'], heard) == heard


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('driver, own', [('2209', tmc.Chopper(2, 4, 5, 1)),
                                         ('5160', tmc.Chopper(2, 4, 5, 3))])
def test_apply_of_the_klipper_defaults_says_the_demo_has_nothing_to_play_with_them(
        source, driver, own, monkeypatch):
    # motor A on a noisy set of its own, and the defaults beat it. A run without TPFD= sets no
    # tpfd: on a TMC5160 the config's stays, here the stock one
    require(source)
    stock = tmc.DRIVERS[driver].default
    monkeypatch.setattr(klipper_front.Front, 'vibration', quiet_at(stock.fields()))
    mk = klipper_front.FrontMoonraker(source, tuned(printer_cfg(driver=driver), driver,
                                                    stepper_x=own))
    root = collect_motor(mk, 'x', ['collect', '--speed', '60', '--tbl', '2:2', '--toff', '3:4',
                                   '--hstrt', '5:5', '--hend', '%d:%d' % (stock.hend, stock.hend + 1),
                                   '--iterations', '1', '--yes', '--no-raw'])
    winner = replace(stock, tpfd=None)
    assert Dataset.open(root).manifest()['winner'] == winner.fields()
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=A APPLY=1')
    assert told == ['Chopper: Motor A: %s from run %s is set on stepper_x until Klipper restarts. '
                    'These are the Klipper defaults. CHOPPER_DEMO refuses to start while stepper_x '
                    'runs them. After you save them, it has nothing to play against the defaults. '
                    'To save them: CHOPPER_ANALYZE DATASET=%s SAVE=1'
                    % (winner.label(), root.rsplit('/', 1)[-1], root)]
    assert held(mk.front.chips['tmc%s stepper_x' % driver], winner) == winner


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('removed', [False, True],
                         ids=['autotune in the config', 'autotune taken out since the run'])
def test_apply_where_klipper_tmc_autotune_manages_names_no_config_and_no_save(source, removed,
                                                                              monkeypatch):
    # autotune writes its own registers at every start: the driver_* lines, here the very
    # winner, say nothing of what a restart brings, and a save would not reach the driver.
    # Taken out of the config since, autotune still measured the run: its save refuses
    require(source)
    cfg = tuned(printer_cfg(), stepper_x=GRID_WINNER) \
        + '\n[autotune_tmc stepper_x]\nmotor: ldo-42sth48-2004ac\n'
    mk = klipper_front.FrontMoonraker(source, cfg)
    root = collect_motor(mk, 'x')
    if removed:
        mk.files['printer.cfg'] = printer_cfg()
        mk.gcode('RESTART')
    assert analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=A APPLY=1') == [
        'Chopper: Motor A: %s from run %s is set on stepper_x until Klipper restarts'
        % (GRID_WINNER.label(), root.rsplit('/', 1)[-1])]
    if removed:
        with pytest.raises(SystemExit, match=r'^not saving \[tmc2209 stepper_x\]: measured under '
                                             r'autotune'):
            analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=A SAVE=1')


@pytest.mark.parametrize('source', SOURCES)
def test_analyze_of_a_motor_takes_its_newest_run_that_measured(source, monkeypatch, capsys):
    # motor A ran after B, and a run of B stopped at once: MOTOR=B takes the B run that measured
    require(source)
    mk = klipper_front.FrontMoonraker(source, printer_cfg())
    stock = chips(mk.front)
    with pytest.raises(SystemExit,
                       match='^tune motor B first: none of its tuning runs has measurements$'):
        analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1')
    root = collect_motor(mk, 'y')
    # without MOTOR= ANALYZE would take the newest run of any motor: the hint names it
    assert hints(mk, root) == [
        'Chopper: To hear the registers this run found, set them live: '
        'CHOPPER_ANALYZE MOTOR=B APPLY=1',
        'Chopper: To save the registers this run found: CHOPPER_SAVE MOTOR=B']
    other = collect_motor(mk, 'x')
    stopped_run(mk, monkeypatch, 'y')
    capsys.readouterr()
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=B APPLY=1')
    assert told[0].startswith('Chopper: Motor B: %s from run %s is set on stepper_y '
                              % (GRID_WINNER.label(), root.rsplit('/', 1)[-1]))
    assert 'motor B: skipping run stopped: it has no measurements' in capsys.readouterr().out
    # without MOTOR= the newest run of any motor: the stopped one
    with pytest.raises(SystemExit, match='^add MOTOR=A or MOTOR=B: the newest run, stopped, has no '
                                         'successful measurements. With MOTOR=, CHOPPER_ANALYZE '
                                         'takes the newest run of that motor that has them$'):
        analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE APPLY=1')
    assert held(mk.front.chips['tmc2209 stepper_y'], GRID_WINNER) == GRID_WINNER
    assert chips(mk.front)['tmc2209 stepper_x'] == stock['tmc2209 stepper_x']
    with pytest.raises(SystemExit, match='^drop DATASET= or MOTOR=: %s is a run of motor A, not '
                                         'of motor B$' % other):
        analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE DATASET=%s MOTOR=B APPLY=1' % other)
    for refusal in ('tune motor B first', 'add MOTOR=A or MOTOR=B', 'drop DATASET= or MOTOR='):
        assert collect.failure_display('analyze FAILED: ' + refusal)[:16] \
            == ('FAIL ' + refusal)[:16]
    assert_clean(mk.front)


@pytest.mark.parametrize('source', SOURCES)
def test_analyze_of_a_run_named_without_measurements_names_that_run(source, monkeypatch):
    # DATASET= or MOTOR= chose the run: adding MOTOR= would change nothing
    require(source)
    mk = klipper_front.FrontMoonraker(source, printer_cfg())
    stopped_run(mk, monkeypatch, 'y')
    stopped = str(collect.RESULTS_HOME / 'datasets' / 'stopped')
    with pytest.raises(SystemExit, match='^no successful measurements in %s$' % re.escape(stopped)):
        analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE DATASET=%s APPLY=1' % stopped)
    # a run of motor A that keeps its winner but lost the file of its measurements
    root = collect_motor(mk, 'x')
    Dataset.open(root).records_path.unlink()
    with pytest.raises(SystemExit, match='^no successful measurements in %s$' % re.escape(root)):
        analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=A APPLY=1')


@pytest.mark.parametrize('source', SOURCES)
def test_a_run_ending_on_the_loaded_registers_ends_well_where_moonraker_does_not_answer(
        source, monkeypatch, capsys):
    # the config files come through Moonraker: unread, the config Klipper loaded decides, and
    # a run that measured to the end does not fail on its hints
    require(source)
    weight, jitter, best, kept = ON_STOCK['kept']
    monkeypatch.setattr(klipper_front.Front, 'vibration', quiet_at(best, weight, jitter))
    front = klipper_front.Front(source, printer_cfg())
    monkeypatch.setattr(collect, 'Klippy', lambda path: types.SimpleNamespace(connect=front.connect))
    monkeypatch.setattr(collect, 'find_socket', lambda explicit=None: '<front>')
    url = 'http://127.0.0.1:1'
    assert collect.run_collect(build_parser().parse_args(AROUND_STOCK + ['--axis', 'x',
                                                                         '--url', url])) == 0
    assert_clean(front)
    root, = (collect.RESULTS_HOME / 'datasets').iterdir()
    assert Dataset.open(str(root)).manifest()['kept']
    assert not [line for line in front.console if 'CHOPPER_' in line]
    assert 'cannot reach Moonraker at %s' % url in capsys.readouterr().out


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('case', ON_STOCK)
@pytest.mark.parametrize('edited', [False, True], ids=['files hold it', 'files edited'])
def test_a_run_ending_on_the_registers_klipper_runs_offers_a_save_only_if_a_restart_changes_them(
        source, case, edited, monkeypatch):
    # motor A on Klipper defaults, registers that barely matter: the run ends on them, kept
    # or found the best. printer.cfg edited since Klipper started gives another set: a
    # restart would lose the run's result, so every command says how to save it
    require(source)
    weight, jitter, best, kept = ON_STOCK[case]
    monkeypatch.setattr(klipper_front.Front, 'vibration', quiet_at(best, weight, jitter))
    mk = klipper_front.FrontMoonraker(source, printer_cfg())
    if edited:
        mk.files['printer.cfg'] = tuned(mk.files['printer.cfg'], stepper_x=GRID_WINNER)
    stock = chips(mk.front)
    root = collect_motor(mk, 'x', AROUND_STOCK)
    manifest = Dataset.open(root).manifest()
    assert (manifest['winner'], manifest['kept']) == (STOCK.fields(), kept)
    name = root.rsplit('/', 1)[-1]
    assert hints(mk, root) == ([
        'Chopper: To hear the registers this run found, set them live: '
        'CHOPPER_ANALYZE MOTOR=A APPLY=1',
        'Chopper: To save the registers this run found: CHOPPER_SAVE MOTOR=A'] if edited else [])
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=A APPLY=1')
    assert told == ['Chopper: Motor A: %s from run %s is set on stepper_x' % (STOCK.label(), name)
                    + (' until Klipper restarts. Klipper runs them from the config it loaded. At '
                       'its next start, Klipper loads other registers from its config files. To '
                       'save them: '
                       'CHOPPER_ANALYZE DATASET=%s SAVE=1' % root if edited else
                       '. These are the registers Klipper loaded from its config at its last '
                       'start. The run found nothing to change')]
    assert chips(mk.front) == stock
    said = save_through(mk, monkeypatch, '--motor', 'a')
    if edited:
        assert said == ['Chopper: motor A: saving %s from run %s' % (STOCK.label(), name)]
        assert mk.restarts == 1 and chips(mk.front) == stock
    else:
        assert said == ['Chopper: motor A: ' + (
            'your registers %s stay. In run %s, neither the winner nor Klipper defaults was '
            'quieter than them in all 4 rounds' if kept else
            'the config already has %s, the result of run %s, so there is nothing to write')
            % (STOCK.label(), name)]
        assert mk.restarts == 0


TUNED = {'x': tmc.Chopper(2, 4, 5, 3), 'y': tmc.Chopper(2, 5, 5, 2)}
LIVE = tmc.Chopper(1, 5, 4, 3)          # set by APPLY=1 or SET_TMC_FIELD, not in the config


def run_demo(front, monkeypatch, argv: 'list[str]', kl=None) -> int:
    """CHOPPER_DEMO through its entry, on our client on the printer (kl: another client of
    it). On both motors the --speed of argv is the speed each motor's last tune recorded
    (known_speed): CHOPPER_DEMO takes SPEED= only with MOTOR=A or MOTOR=B."""
    kl = kl or front.connect()
    monkeypatch.setattr(demo, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(demo, 'find_socket', lambda explicit=None: '<front>')
    args = build_parser().parse_args(['demo'] + argv)
    if args.axis == 'xy' and args.speed is not None:
        recorded, args.speed = args.speed, None
        monkeypatch.setattr(demo, 'known_speed', lambda axis: recorded)
    return demo.run_demo(args)


def sent_since(front, start: int) -> 'list[str]':
    """The G-code lines the tools sent after the first `start` scripts, the fences of
    gcode_output left out."""
    return [line for script in front.scripts[start:] for line in script.split('\n')
            if not line.startswith('ECHO CHOPPER-')]


SET_LIVE = {
    # (the drivers' registers in printer.cfg, the drivers set live, the argv)
    'motor A on the defaults': ({}, ('stepper_x',), ['--motor', 'a', '--speed', '60', '--rounds', '1']),
    'report of A on the defaults': ({}, ('stepper_x',), ['--motor', 'a', '--speed', '60', '--report']),
    'show, A on the defaults': ({'stepper_y': TUNED['y']}, ('stepper_x',), ['--speed', '60']),
    'show, both on the defaults': ({}, ('stepper_x',), ['--speed', '60']),
    'motor A on a set of its own': ({'stepper_x': TUNED['x']}, ('stepper_x',),
                                    ['--motor', 'a', '--speed', '60', '--rounds', '1']),
    # the resonance scan would end on the config's registers before the check
    'motor A with no speed to reuse': ({'stepper_x': TUNED['x']}, ('stepper_x',),
                                       ['--motor', 'a', '--rounds', '1']),
    'show, B set live': ({'stepper_x': TUNED['x'], 'stepper_y': TUNED['y']}, ('stepper_y',),
                         ['--speed', '60']),
    'show, both set live': ({'stepper_x': TUNED['x'], 'stepper_y': TUNED['y']},
                            ('stepper_x', 'stepper_y'), ['--speed', '60']),
    'show, both on the defaults and set live': ({}, ('stepper_x', 'stepper_y'), ['--speed', '60']),
    # the report plays motor A first: motor B is read before it
    'report of both, B set live': ({'stepper_x': TUNED['x'], 'stepper_y': TUNED['y']},
                                   ('stepper_y',), ['--speed', '60', '--report']),
}


def set_live_refusal(config: dict, steppers: 'tuple[str, ...]') -> str:
    """The refusal of a demo whose `steppers` run LIVE, `config` giving their registers."""
    gives = {}
    for stepper in steppers:
        gives.setdefault(config.get(stepper, STOCK).label(), []).append(stepper)
    runs = '; '.join('%s, but the config that Klipper loaded gives %s %s' % (
        '%s and %s run %s' % (*group, LIVE.label()) if len(group) > 1 else
        '%s runs %s' % (group[0], LIVE.label()), 'them' if len(group) > 1 else 'it', label)
        for label, group in gives.items())
    analyze = ('repeat for each motor the CHOPPER_ANALYZE that set its registers, with MOTOR=A or '
               'MOTOR=B added' if len(steppers) > 1 else
               'repeat the CHOPPER_ANALYZE that set them, with MOTOR=%s added'
               % {'stepper_x': 'A', 'stepper_y': 'B'}[steppers[0]])
    return ('save or drop the registers set live, then retry: %s. The demo plays the registers of '
            'that config. At its end it puts them back, which would undo the ones set live. To '
            'keep them, %s and SAVE=1 in place of APPLY=1. Or write them into the config and '
            'restart Klipper (RESTART). To drop them, restart Klipper. Nothing was moved'
            % (runs, analyze))


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('case', SET_LIVE)
def test_a_demo_of_drivers_set_live_unlike_the_config_is_refused_before_anything_moves(
        source, case, monkeypatch):
    # the demo plays the config's registers and puts them back at its end: it would play
    # the wrong set and undo the live one
    require(source)
    config, steppers, argv = SET_LIVE[case]
    front = klipper_front.Front(source, tuned(printer_cfg(), **config))
    front.run('\n'.join(tmc.set_fields_script(stepper, LIVE.fields()) for stepper in steppers))
    live, start = chips(front), len(front.scripts)
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, argv)
    assert refused.value.code == set_live_refusal(config, steppers)
    assert collect.failure_display('demo FAILED: %s' % refused.value.code)[:16] == 'FAIL save or dro'
    assert_clean(front)
    assert front.moves == [] and chips(front) == live
    assert {line.split()[0] for line in sent_since(front, start)} == {'DUMP_TMC'}


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('argv', [['--speed', '60'], ['--speed', '60', '--report'],
                                  ['--motor', 'b', '--rounds', '1']],
                         ids=['show', 'report of both', 'motor B with no speed to reuse'])
def test_a_demo_while_printing_refuses_before_it_reads_a_driver(source, argv, monkeypatch):
    # the refusal of registers set live says to restart Klipper, which would end the print
    require(source)
    front = klipper_front.Front(source, tuned(printer_cfg(), stepper_x=TUNED['x'],
                                              stepper_y=TUNED['y']))
    front.run(tmc.set_fields_script('stepper_y', LIVE.fields()))
    start = len(front.scripts)
    kl = front.connect()
    kl.is_printing = lambda: True
    with pytest.raises(collect.PrinterBusy, match='busy printing'):
        run_demo(front, monkeypatch, argv, kl)
    assert sent_since(front, start) == [] and front.moves == []


@pytest.mark.parametrize('source', SOURCES)
def test_a_driver_whose_registers_klipper_cannot_read_is_not_compared(source, monkeypatch):
    # both drivers set live; Klipper fails to read stepper_y's CHOPCONF: the refusal names
    # only what it read
    require(source)
    front = klipper_front.Front(source, tuned(printer_cfg(), stepper_x=TUNED['x'],
                                              stepper_y=TUNED['y']))
    front.run('\n'.join(tmc.set_fields_script(stepper, LIVE.fields())
                        for stepper in ('stepper_x', 'stepper_y')))
    chip = front.chips['tmc2209 stepper_y']
    read = chip.get_register

    def get_register(reg_name):
        if reg_name == 'CHOPCONF':
            raise front.printer.command_error("Unable to read tmc uart 'stepper_y' register "
                                              "CHOPCONF")
        return read(reg_name)
    chip.get_register = get_register
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, ['--speed', '60'])
    assert refused.value.code == set_live_refusal({'stepper_x': TUNED['x']}, ('stepper_x',))
    assert front.moves == []


@pytest.mark.parametrize('source', SOURCES)
def test_the_registers_applied_without_a_motor_are_kept_by_the_analyze_the_demo_names(
        source, monkeypatch):
    # APPLY=1 without MOTOR= on motor A's run, then a run of motor B: without MOTOR=, the
    # newest run of any motor, motor B's, is the one CHOPPER_ANALYZE SAVE=1 would save
    require(source)
    monkeypatch.setattr(klipper_front.Front, 'vibration', quiet_at({'toff': 4, 'hend': 1}))
    mk = klipper_front.FrontMoonraker(source, printer_cfg())
    collect_motor(mk, 'x')
    applied = 'CHOPPER_ANALYZE APPLY=1'
    analyze_through(mk, monkeypatch, applied)
    collect_motor(mk, 'y', AROUND_STOCK)
    assert held(mk.front.chips['tmc2209 stepper_x'], GRID_WINNER) == GRID_WINNER
    with pytest.raises(SystemExit) as refused:
        run_demo(mk.front, monkeypatch, ['--speed', '60', '--rounds', '1'])
    assert 'To keep them, repeat the CHOPPER_ANALYZE that set them, with MOTOR=A added and SAVE=1 ' \
           'in place of APPLY=1.' in refused.value.code
    analyze_through(mk, monkeypatch, applied.replace('APPLY=1', 'MOTOR=A SAVE=1'))
    assert mk.restarts == 1
    assert held(mk.front.chips['tmc2209 stepper_x'], GRID_WINNER) == GRID_WINNER
    assert held(mk.front.chips['tmc2209 stepper_y'], STOCK) == STOCK


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('dry_run', [True, False], ids=['dry run', 'run'])
@pytest.mark.parametrize('argv, motors', [(['--motor', 'a'], 'x'), ([], 'xy')],
                         ids=['motor A', 'show'])
def test_a_demo_on_the_defaults_names_where_they_come_from(source, dry_run, argv, motors,
                                                           monkeypatch):
    # printer.cfg edited without a restart is not what Klipper loaded: the refusal says so
    require(source)
    front = klipper_front.Front(source, printer_cfg())
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, ['--speed', '60'] + argv + ['--dry-run'] * dry_run)
    assert refused.value.code == (
        'tune and save %s first: the config that Klipper loaded gives %s. '
        "So the demo's defaults and tuned registers would be the same. If you edited the config "
        'after Klipper started, restart Klipper (RESTART). Nothing was moved'
        % ('motor A' if motors == 'x' else 'motors A and B', ', and '.join(
            'the Klipper defaults %s to stepper_%s' % (STOCK.label(), axis) for axis in motors)))
    assert collect.failure_display('demo FAILED: %s' % refused.value.code)[:16] == 'FAIL tune and sa'
    # the drivers read before the refusal, and nothing else
    assert sent_since(front, 0) == ([] if dry_run else [
        'DUMP_TMC STEPPER=stepper_%s REGISTER=CHOPCONF' % axis for axis in motors])


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('dry_run', [True, False], ids=['dry run', 'run'])
@pytest.mark.parametrize('argv, motors', [(['--motor', 'b'], 'y'), ([], 'xy')],
                         ids=['motor B', 'show'])
def test_a_default_equal_to_the_saved_set_asks_for_another_default(source, dry_run, argv, motors,
                                                                    monkeypatch):
    # both motors tuned and saved to motor B's set: DEFAULT= that set plays it as DEFAULTS and TUNED
    require(source)
    front = klipper_front.Front(source, tuned(printer_cfg(), stepper_x=TUNED['y'],
                                              stepper_y=TUNED['y']))
    with pytest.raises(SystemExit) as refused:
        run_demo(front, monkeypatch, ['--speed', '60', '--default', '2,5,5,2'] + argv
                 + ['--dry-run'] * dry_run)
    assert refused.value.code == (
        'pass another DEFAULT= or drop it, then retry: the config that Klipper loaded gives %s. '
        "So the demo's defaults and tuned registers would be the same. Nothing was moved"
        % ', and '.join('the DEFAULT= registers %s to stepper_%s' % (TUNED['y'].label(), axis)
                        for axis in motors))
    assert collect.failure_display('demo FAILED: %s' % refused.value.code)[:16] == 'FAIL pass anothe'
    assert front.moves == [] and {line.split()[0] for line in sent_since(front, 0)} <= {'DUMP_TMC'}


@pytest.mark.parametrize('source', SOURCES)
def test_the_show_with_a_default_equal_to_one_saved_set_names_it(source, monkeypatch, capsys):
    # motor A changes; motor B plays its saved set, the one DEFAULT= gives, as DEFAULTS and TUNED
    require(source)
    front = klipper_front.Front(source, tuned(printer_cfg(), stepper_x=TUNED['x'],
                                              stepper_y=TUNED['y']))
    assert run_demo(front, monkeypatch, ['--speed', '60', '--default', '2,5,5,2', '--dry-run']) == 0
    assert ('Motor B plays the same registers in DEFAULTS and TUNED. The config that Klipper '
            'loaded gives the DEFAULT= registers %s to stepper_y. Pass another DEFAULT= or drop it to '
            'hear motor B change too\n' % TUNED['y'].label()) in capsys.readouterr().out


@pytest.mark.parametrize('source', SOURCES)
def test_the_show_names_no_motor_of_klipper_tmc_autotune_as_playing_the_same_registers(
        source, monkeypatch, capsys):
    # autotune writes its own registers into motor B's driver: no driver_* lines say them
    require(source)
    cfg = tuned(printer_cfg(), stepper_x=TUNED['x']) \
        + '\n[autotune_tmc stepper_y]\nmotor: ldo-42sth48-2004ac\n'
    front = klipper_front.Front(source, cfg)
    assert run_demo(front, monkeypatch, ['--speed', '60', '--dry-run']) == 0
    out = capsys.readouterr().out
    assert 'Show on both motors together' in out and 'plays the same registers' not in out


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('dry_run', [True, False], ids=['dry run', 'run'])
def test_the_show_says_which_motor_plays_the_same_registers_in_both_rounds(source, dry_run,
                                                                           monkeypatch, capsys):
    # motor A tuned and saved, motor B on Klipper defaults: the demo of only motor B has nothing
    # to play
    require(source)
    front = klipper_front.Front(source, tuned(printer_cfg(), stepper_x=TUNED['x']))
    assert run_demo(front, monkeypatch, ['--speed', '60', '--rounds', '1', '--repeats', '1']
                    + ['--dry-run'] * dry_run) == 0
    assert_clean(front)
    out = capsys.readouterr().out
    same = ('Motor B plays the same registers in DEFAULTS and TUNED. The config that Klipper '
            'loaded gives the Klipper defaults %s to stepper_y. Tune and save motor B to hear it change '
            'too' % STOCK.label())
    assert same + '\n' in out
    if dry_run:
        return
    # the show runs detached: its log is unseen
    first_round = next(index for index, line in enumerate(front.console) if '>> DEFAULTS' in line)
    assert 'Chopper: ' + same in front.console[:first_round]
    assert re.search(r'\nboth motors together: \d+\.\d\dx less vibration overall; motor B played '
                     r'the same registers in DEFAULTS and TUNED\n', out)
    verdict = max(index for index, line in enumerate(front.console)
                  if line.startswith('echo: Chopper: both motors '))
    assert front.console[verdict].endswith(
        'x less vibration; motor B played the same registers in DEFAULTS and TUNED')
    assert front.console[verdict + 1:] == [
        'Chopper: To see the gain of motor A, play only that motor: CHOPPER_DEMO MOTOR=A ROUNDS=1 '
        'REPEATS=1']


@pytest.mark.parametrize('source', SOURCES)
def test_the_show_prints_a_small_gain_with_two_decimals_and_names_the_demo_of_each_motor(
        source, monkeypatch, capsys):
    # tavalin's show: motor B quieter a little, motor A not at all. One decimal printed 1.0x,
    # and the panel showed the gain of both together as the gain of each motor
    require(source)
    shakes = {STOCK.toff: 500., TUNED['x'].toff: 500., TUNED['y'].toff: 470.}
    monkeypatch.setattr(klipper_front.Front, 'shake',
                        staticmethod(lambda speed, chopper: shakes[chopper['toff']]))
    front = klipper_front.Front(source, tuned(printer_cfg(), stepper_x=TUNED['x'],
                                              stepper_y=TUNED['y']))
    assert run_demo(front, monkeypatch, ['--speed', '60', '--rounds', '1', '--repeats', '1']) == 0
    assert_clean(front)
    out = capsys.readouterr().out
    # both vibrations of the round and their ratio, 1000/970 mg
    before, after = map(int, re.search(r'\n   => both motors, defaults (\d+) -> tuned (\d+): '
                                       r'1\.03x less vibration\n', out).groups())
    assert before / after == pytest.approx(1000 / 970, abs=0.005)
    assert '\nboth motors together: 1.03x less vibration overall\n' in out
    verdict = front.console.index('echo: Chopper: both motors together 1.03x less vibration')
    each = front.console[verdict + 1:]
    assert each == ['Chopper: To see the gain of motor %s, play only that motor: CHOPPER_DEMO '
                    'MOTOR=%s ROUNDS=1 REPEATS=1' % (motor, motor) for motor in 'AB']
    shown = [script[len('M117 '):] for script in front.scripts if script.startswith('M117 ')]
    assert shown[-1] == '1.03x less vib'
    assert not (collect.RESULTS_HOME / 'state.json').exists()       # the panel: no gain per motor
    # pasted, each line plays only its motor: its own gain, which the panel shows
    for line, axis, gain in zip(each, 'xy', ('1.00', '1.06')):
        kl = front.connect()                            # each run closes its client
        monkeypatch.setattr(demo, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
        assert demo.run_demo(pasted(line)) == 0
        assert_clean(front)
        motor = collect.motor_label(axis)
        # two such windows in a row: each names its motor
        assert front.console[-1] == ('echo: Chopper demo: motor %s, %sx less vibration'
                                     % (motor, gain))
        assert [script for script in front.scripts if script.startswith('M117 ')][-1] \
            == 'M117 %s %sx less vib' % (motor, gain)
        state = json.loads((collect.RESULTS_HOME / 'state.json').read_text())
        assert state[axis] == {'regs': '/'.join(map(str, TUNED[axis].fields().values())),
                               'quieter': float(gain), 'own': True}
    shown = [script[len('M117 '):] for script in front.scripts if script.startswith('M117 ')]
    assert max(len(text) for text in shown) <= collect.LCD_WIDTH


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('tuned_shake, gain, shown', [
    # two decimals would make 'A 12.50x less vib', a character past the display
    (40., '12.50x less vibration', 'A 12.5x less vib'),
    (510., '1.02x more vibration', 'A 1.02x more vib')], ids=['12.5x less', 'more'])
def test_the_demo_of_a_motor_says_less_or_more_vibration_and_fits_the_display(
        source, tuned_shake, gain, shown, monkeypatch, capsys):
    require(source)
    shakes = {STOCK.toff: 500., TUNED['x'].toff: tuned_shake}
    monkeypatch.setattr(klipper_front.Front, 'shake',
                        staticmethod(lambda speed, chopper: shakes[chopper['toff']]))
    front = klipper_front.Front(source, tuned(printer_cfg(), stepper_x=TUNED['x']))
    assert run_demo(front, monkeypatch, ['--motor', 'a', '--speed', '60', '--rounds', '1',
                                         '--repeats', '1']) == 0
    assert_clean(front)
    out = capsys.readouterr().out
    assert re.search(r'\n   => motor A, defaults \d+ -> tuned \d+: %s\n' % gain, out)
    assert '\n  %s overall\n' % gain in out
    assert re.search(r'\n  \d+\.\d\dx %s above the \d+ noise floor\n' % gain.split('x ')[1], out)
    assert front.console[-1] == 'echo: Chopper demo: motor A, ' + gain
    # the round, then the result
    assert [script for script in front.scripts if script.startswith('M117 A ')][-2:] \
        == ['M117 ' + shown] * 2
    assert len(shown) == collect.LCD_WIDTH


@pytest.mark.parametrize('source', SOURCES)
def test_a_driver_switched_off_without_an_enable_pin_of_its_own_is_no_register_set_live(
        source, monkeypatch):
    # Klipper switches such a driver off with toff 0 and puts the config's toff back when the
    # motor goes on: the demo plays as the config says
    require(source)
    cfg = tuned(printer_cfg(), stepper_x=TUNED['x']).replace('enable_pin: !P20\n', '')
    front = klipper_front.Front(source, cfg)
    front.run('M84')
    assert front.chips['tmc2209 stepper_x'].field('toff') == 0
    assert run_demo(front, monkeypatch, ['--motor', 'a', '--speed', '60', '--rounds', '1',
                                         '--repeats', '1']) == 0
    assert_clean(front)
    assert {move['chips']['tmc2209 stepper_x']['toff'] for move in front.moves} == {3, 4}


def demo_records() -> 'list[dict]':
    """The measurements of the demo's dataset: it takes no DATASET=, the run makes it."""
    root, = [root for root in (collect.RESULTS_HOME / 'datasets').iterdir() if '_demo_' in root.name]
    return Dataset.open(root).moves()


@pytest.mark.parametrize('source', SOURCES)
def test_registers_applied_play_in_the_demo_once_saved(source, monkeypatch):
    # meijjaa's way: the search, APPLY=1, the demo, the save the APPLY line names
    require(source)
    mk = klipper_front.FrontMoonraker(source, printer_cfg())
    collect_motor(mk, 'x')
    told = analyze_through(mk, monkeypatch, 'CHOPPER_ANALYZE MOTOR=A APPLY=1')
    argv = ['--motor', 'a', '--speed', '60', '--rounds', '1', '--repeats', '1']
    with pytest.raises(SystemExit, match='^save or drop the registers set live'):
        run_demo(mk.front, monkeypatch, argv)
    assert held(mk.front.chips['tmc2209 stepper_x'], GRID_WINNER) == GRID_WINNER
    analyze_through(mk, monkeypatch, told[0])
    assert mk.restarts == 1
    assert run_demo(mk.front, monkeypatch, argv) == 0
    assert_clean(mk.front)
    assert {tmc.Chopper(record['tbl'], record['toff'], record['hstrt'], record['hend'])
            for record in demo_records()} == {STOCK, GRID_WINNER}
    assert held(mk.front.chips['tmc2209 stepper_x'], GRID_WINNER) == GRID_WINNER


SPEED_FOR = {
    # (the drivers' registers in printer.cfg, the motors with a speed from their last tune, the
    # motor SPEED= is for)
    'B without a speed': ({'stepper_x': TUNED['x'], 'stepper_y': TUNED['y']}, {'x': 60}, 'B'),
    'B without a speed on the defaults': ({'stepper_x': TUNED['x']}, {'x': 60}, 'A'),
    'A without a speed on the defaults': ({'stepper_y': TUNED['y']}, {'y': 60}, 'B'),
    'both on the defaults': ({}, {}, None),
}


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('case', SPEED_FOR)
def test_a_speed_on_both_motors_names_the_demo_of_a_motor_with_something_to_play(source, case,
                                                                                monkeypatch):
    # the show of both refuses a motor without a speed, the report scans it: SPEED= is for it,
    # unless its config gives it the defaults and its demo refuses too
    require(source)
    config, recorded, motor = SPEED_FOR[case]
    front = klipper_front.Front(source, tuned(printer_cfg(), **config))
    monkeypatch.setattr(demo, 'known_speed', recorded.get)
    monkeypatch.setattr(demo, 'Klippy', lambda path: types.SimpleNamespace(connect=front.connect))
    monkeypatch.setattr(demo, 'find_socket', lambda explicit=None: '<front>')
    with pytest.raises(SystemExit) as refused:
        demo.run_demo(build_parser().parse_args(['demo', '--speed', '40', '--report',
                                                 '--iterations', '1']))
    if motor is None:
        assert refused.value.code.startswith('tune and save motors A and B first: ')
        return
    line = 'CHOPPER_DEMO MOTOR=%s SPEED=40 REPORT=1 ITERATIONS=1' % motor
    assert refused.value.code.endswith(' To play only motor %s at this speed: %s' % (motor, line))
    # pasted, it plays that motor at that speed
    assert demo.run_demo(pasted(line)) == 0
    assert_clean(front)
    assert {(move['stepper'], move['speed']) for move in front.moves} \
        == {('stepper_' + {'A': 'x', 'B': 'y'}[motor], 40)}


@pytest.mark.parametrize('source', SOURCES)
def test_a_speed_on_both_motors_set_live_asks_to_save_or_drop_them_first(source, monkeypatch):
    # the demo of one motor that the refusal of SPEED= would name refuses them too
    require(source)
    front = klipper_front.Front(source, tuned(printer_cfg(), stepper_x=TUNED['x'],
                                              stepper_y=TUNED['y']))
    front.run(tmc.set_fields_script('stepper_y', LIVE.fields()))
    monkeypatch.setattr(demo, 'known_speed', {'x': 60}.get)
    monkeypatch.setattr(demo, 'Klippy', lambda path: types.SimpleNamespace(connect=front.connect))
    monkeypatch.setattr(demo, 'find_socket', lambda explicit=None: '<front>')
    with pytest.raises(SystemExit) as refused:
        demo.run_demo(build_parser().parse_args(['demo', '--speed', '40']))
    assert refused.value.code == set_live_refusal({'stepper_y': TUNED['y']}, ('stepper_y',))


@pytest.mark.parametrize('source', SOURCES)
def test_a_report_of_both_motors_names_the_demo_of_the_motor_it_skips(source, monkeypatch, capsys):
    # SPEED= with both motors refuses: the hints name the demo of one motor
    require(source)
    front = klipper_front.Front(source, tuned(printer_cfg(), stepper_x=TUNED['x'],
                                              stepper_y=TUNED['y']))
    monkeypatch.setattr(demo, 'known_speed', {'x': 60}.get)
    monkeypatch.setattr(find_speed, 'scan', lambda kl, args, popup=True: (0, None, 64))
    assert run_demo(front, monkeypatch, ['--report', '--iterations', '1']) == 2
    assert_clean(front)
    out = capsys.readouterr().out.splitlines()
    assert 'Using resonance speed 60 mm/s from the last motor A run (pass SPEED= with MOTOR=A to ' \
           'override)' in out
    skipped = 'motor B skipped: no clear resonance speed found. To play motor B at the speed it ' \
              'vibrates most: CHOPPER_DEMO MOTOR=B SPEED=64 REPORT=1 ITERATIONS=1'
    assert skipped in out
    # pasted, it plays motor B at that speed
    args = pasted(skipped)
    assert (args.axis, args.speed, args.report) == ('y', 64, True)
    kl = front.connect()
    monkeypatch.setattr(demo, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    assert demo.run_demo(args) == 0
    assert {move['speed'] for move in front.moves if move['stepper'] == 'stepper_y'} == {64}
