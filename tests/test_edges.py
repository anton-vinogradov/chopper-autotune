"""The edges of the register space: TBL 0 and the datasheet's top effective hysteresis 16.
Winners on both at once vibrated least at the resonance yet were noisy at 5 and 10 mm/s,
so a register search leaves them out unless told otherwise. The ranges and the hysteresis
cap bound everything a run tries and recommends: the start of the descent, your registers
in the back-to-back rounds, the ranking of a resumed dataset."""
from dataclasses import fields, replace

import pytest

from chopper_autotune import collect, tmc, tune
from chopper_autotune import extruder as extruder_mod
from chopper_autotune.analyze import run_analyze
from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
from chopper_autotune.dataset import Dataset

DRIVER = tmc.DRIVERS['2209']
EDGES = ('--tbl', '0:3', '--max-hysteresis', '16')


def macro(*params: str):
    parser = build_parser()
    return parser.parse_args(_gcode_args(list(params), boolean_flags(parser)))


def on_edge(combo: tmc.Chopper) -> bool:
    return combo.tbl == 0 or tmc.effective_hysteresis(combo) == 16


def edgy(combo: tmc.Chopper, iteration: int = 0) -> float:
    """Quietest at TBL 0 and at hysteresis 16, each edge worth a clear win."""
    return 100.0 + 20 * combo.tbl + 10 * abs(combo.toff - 3) \
        + 20 * (16 - tmc.effective_hysteresis(combo))


def with_motor(printer, monkeypatch, driver: str, baseline: dict):
    stub = collect.detect_hardware
    monkeypatch.setattr(collect, 'detect_hardware', lambda kl, axis: replace(
        stub(kl, axis), driver=tmc.DRIVERS[driver], baseline=dict(baseline)))


def winner(printer) -> tmc.Chopper:
    return tmc.Chopper(**Dataset.open(printer.roots[-1]).manifest()['winner'])


def run_collect(printer, *argv: str) -> dict:
    args = build_parser().parse_args(['collect', '--axis', 'x', '--speed', '58', '--search',
                                      'descent', '--yes', '--no-raw', *argv])
    collect.collect(printer.kl(), args)
    return Dataset.open(args.dataset or printer.roots[-1]).manifest()


def run_tune(*argv: str):
    tune.run_tune(build_parser().parse_args(['tune', '--motor', 'a', '--speed', '58', '--no-raw',
                                             *argv]))


# 1. by default no search tries or recommends an edge; the defaults live in one place

@pytest.mark.parametrize('command', ['tune', 'collect', 'extruder'])
def test_every_register_search_defaults_to_the_one_space_without_the_edges(command):
    space = tmc.Space.of(build_parser().parse_args([command, '--speed', '58']))
    tpfd = {'tune': tmc.SEARCH_SPACE.tpfd, 'collect': None, 'extruder': None}[command]
    assert space == replace(tmc.SEARCH_SPACE, tpfd=tpfd)
    assert space.outside(tmc.Chopper(0, 3, 4, 7)) == 'TBL 0 is outside TBL=1:3'
    assert space.outside(tmc.Chopper(1, 3, 3, 15)) == 'hysteresis 16 is above MAX_HYSTERESIS=15'
    assert space.outside(tmc.Chopper(1, 3, 2, 15)) is None


def test_a_grid_by_default_plans_no_edge():
    plan = collect.build_plan(DRIVER, tmc.Space.of(macro('collect', 'SPEED=58')), [58])
    assert plan and not any(on_edge(combo) for combo, _ in plan)
    assert {combo.tbl for combo, _ in plan} == {1, 2, 3}
    assert max(tmc.effective_hysteresis(combo) for combo, _ in plan) == 15


def test_a_register_search_by_default_measures_and_recommends_no_edge(stub_printer):
    stub_printer.magnitude = edgy
    run_collect(stub_printer)
    assert stub_printer.measured and not any(on_edge(c) for c in stub_printer.measured)
    assert winner(stub_printer).tbl == 1 and tmc.effective_hysteresis(winner(stub_printer)) == 15


def test_a_tune_by_default_measures_and_recommends_no_edge(stub_printer):
    stub_printer.magnitude = edgy
    run_tune()
    assert stub_printer.measured and not any(on_edge(c) for c in stub_printer.measured)
    assert not on_edge(winner(stub_printer))


def run_extruder(monkeypatch, *params: str) -> 'tuple[list[str], list[tmc.Chopper], tmc.Chopper]':
    """A CHOPPER_EXTRUDER at SPEED=5 on a TMC2209 whose moves measure edgy(): the g-code
    it sends, the registers it measures, the winner it remembers."""
    import re
    from types import SimpleNamespace
    sent, measured, winners = [], [], []
    kl = SimpleNamespace(settings=lambda: {'tmc2209 extruder': {}, 'extruder': {}},
                         gcode=sent.append, subscribe_accel=lambda chip: None)
    monkeypatch.setattr(extruder_mod, 'refuse_if_printing', lambda kl: None)
    monkeypatch.setattr(extruder_mod, 'detect_hardware', lambda kl, axis: SimpleNamespace(
        accel_chip='adxl345', display=False))
    monkeypatch.setattr(extruder_mod, 'resolve_extruder_stealth', lambda kl, driver, stealth: None)
    monkeypatch.setattr(extruder_mod, 'save_winner_state',
                        lambda name, winner, *rest: winners.append(winner))

    def measure(hw, speed):
        values = dict(re.findall(r'FIELD=(\w+) VALUE=(\d+)', sent[-1]))
        measured.append(tmc.Chopper(*(int(values[name]) for name in ('tbl', 'toff', 'hstrt', 'hend'))))
        return edgy(measured[-1]), 0
    monkeypatch.setattr(extruder_mod, 'measure', measure)
    extruder_mod.extruder_tune(kl, macro('extruder', 'SPEED=5', 'YES=1', *params))
    return sent, measured, winners[-1]


def test_the_descent_seeds_every_hend_level_inside_the_cap():
    # the hend 15 level sits at hysteresis 16 with the middle HSTRT: it moves to HSTRT 2
    from chopper_autotune.search import _spanning_starts
    seeds = _spanning_starts(tmc.SEARCH_SPACE, DRIVER)
    assert {c.hend for c in seeds} == {0, 7, 15}
    assert not any(tmc.SEARCH_SPACE.outside(c) or tmc.validate(c, DRIVER) for c in seeds)


def test_the_extruder_by_default_measures_and_recommends_no_edge(monkeypatch):
    _, measured, best = run_extruder(monkeypatch)
    assert measured and not any(on_edge(c) for c in measured) and not on_edge(best)


# 2. TBL=0:3 brings TBL 0 back, MAX_HYSTERESIS=16 the hysteresis 16

@pytest.mark.parametrize('argv, tbl, hysteresis', [
    ((), 1, 15),
    (('--tbl', '0:3'), 0, 15),
    (('--max-hysteresis', '16'), 1, 16),
    (EDGES, 0, 16),
])
def test_each_edge_comes_back_only_when_asked(stub_printer, argv, tbl, hysteresis):
    stub_printer.magnitude = edgy
    run_collect(stub_printer, *argv)
    found = winner(stub_printer)
    assert (found.tbl, tmc.effective_hysteresis(found)) == (tbl, hysteresis)
    assert min(c.tbl for c in stub_printer.measured) == tbl
    assert max(tmc.effective_hysteresis(c) for c in stub_printer.measured) == hysteresis


def test_the_extruder_takes_the_edges_back_too(monkeypatch):
    _, _, best = run_extruder(monkeypatch, 'TBL=0:3', 'MAX_HYSTERESIS=16')
    assert best.tbl == 0 and tmc.effective_hysteresis(best) == 16


@pytest.mark.parametrize('command', ['tune', 'collect', 'extruder'])
def test_the_hysteresis_goes_no_higher_than_the_datasheet(command, capsys):
    assert macro(command, 'SPEED=58', 'MAX_HYSTERESIS=16').max_hysteresis == 16
    with pytest.raises(SystemExit):
        macro(command, 'SPEED=58', 'MAX_HYSTERESIS=17')
    assert 'argument --max-hysteresis: expected 16 at most, the datasheet maximum' \
        in capsys.readouterr().err


@pytest.mark.parametrize('command', ['tune', 'collect'])
@pytest.mark.parametrize('value', ['16', '0:16', '16:20'])
def test_tpfd_goes_no_higher_than_its_register(command, value, capsys):
    # SET_TMC_FIELD keeps only the four bits of the register: TPFD 16 would run as 0
    with pytest.raises(SystemExit):
        macro(command, 'MOTOR=A', 'SPEED=58', 'TPFD=' + value)
    assert "argument --tpfd: expected values from 0 to 15, got '%s'" % value \
        in capsys.readouterr().err
    assert macro(command, 'MOTOR=A', 'SPEED=58', 'TPFD=0:15').tpfd == tmc.Range(0, 15)


# 3. CHOPPER_TUNE takes the ranges of CHOPPER_COLLECT

def range_options(command: str) -> 'set[str]':
    import argparse
    names = {field.name for field in fields(tmc.Space)}
    subparsers = next(action for action in build_parser()._actions
                      if isinstance(action, argparse._SubParsersAction))
    return {option for action in subparsers.choices[command]._actions if action.dest in names
            for option in action.option_strings}


def test_tune_takes_every_range_of_collect():
    assert range_options('tune') == range_options('collect') == {
        '--tbl', '--toff', '--hstrt', '--hend', '--tpfd', '--max-hysteresis'}
    assert range_options('extruder') == range_options('collect') - {'--tpfd'}


def test_the_ranges_of_a_tune_reach_its_register_search():
    args = macro('tune', 'MOTOR=A', 'TBL=0:3', 'TOFF=2:6', 'HSTRT=1', 'HEND=0:10', 'TPFD=0:7',
                 'MAX_HYSTERESIS=16')
    search = tune.collect_args(args, 'x', 58, None)
    assert tmc.Space.of(search) == tmc.Space.of(args) == tmc.Space(
        tmc.Range(0, 3), tmc.Range(2, 6), tmc.Range(1, 1), tmc.Range(0, 10), tmc.Range(0, 7), 16)
    assert macro('tune', 'MOTOR=A', 'TBL=1:3').tbl == tmc.Range(1, 3)


def test_a_tune_searches_inside_its_ranges(stub_printer):
    stub_printer.magnitude = edgy
    run_tune('--toff', '4:6', '--hend', '2:9', *EDGES)
    assert {c.toff for c in stub_printer.measured} <= {4, 5, 6}
    assert {c.hend for c in stub_printer.measured} <= set(range(2, 10))
    assert winner(stub_printer).tbl == 0


# 4. the ranges and the cap bound the start of the descent and the rounds

MEIJJAA = {'tbl': 0, 'toff': 2, 'hstrt': 3, 'hend': 15, 'tpfd': 9}       # both edges at once


def quietest_in_the_rounds(combo: tmc.Chopper, iteration: int) -> float:
    """The motor's own registers read loud in the search and quietest in the back-to-back
    rounds, iteration 3 on: the rounds keep them, if they measure them at all."""
    if combo == tmc.Chopper(**MEIJJAA):
        return 100.0 if iteration >= 3 else 300.0
    return 200.0 + 10 * abs(combo.toff - 4) + combo.hend


def test_the_descent_starts_inside_the_ranges_and_saved_edges_stay_out_of_the_rounds(
        stub_printer, monkeypatch, capsys):
    with_motor(stub_printer, monkeypatch, '5160', MEIJJAA)
    stub_printer.magnitude = quietest_in_the_rounds
    run_tune()
    # the nearest combo inside: TBL 1, and HSTRT one lower to keep HEND 15 at hysteresis 15
    assert stub_printer.measured[0] == tmc.Chopper(1, 2, 2, 15, 9)
    assert tmc.Chopper(**MEIJJAA) not in stub_printer.measured
    manifest = Dataset.open(stub_printer.roots[-1]).manifest()
    assert not manifest['kept'] and not on_edge(winner(stub_printer))
    out = capsys.readouterr().out
    assert ('Your registers tbl0_toff2_hstrt3_hend15_tpfd9 are not measured back to back '
            '(TBL 0 is outside TBL=1:3 and hysteresis 16 is above MAX_HYSTERESIS=15): the winner '
            'is not checked against them') in out.splitlines()
    assert 'Keep your registers' not in out


def test_saved_edges_asked_back_start_the_descent_and_can_be_kept(stub_printer, monkeypatch,
                                                                  capsys):
    with_motor(stub_printer, monkeypatch, '5160', MEIJJAA)
    stub_printer.magnitude = quietest_in_the_rounds
    run_tune(*EDGES)
    assert stub_printer.measured[0] == winner(stub_printer) == tmc.Chopper(**MEIJJAA)
    assert Dataset.open(stub_printer.roots[-1]).manifest()['kept']
    assert 'Keep your registers tbl0_toff2_hstrt3_hend15_tpfd9' in capsys.readouterr().out


def test_a_seed_outside_the_ranges_starts_at_the_nearest_combo_inside(stub_printer, tmp_path,
                                                                      capsys):
    seed = Dataset.create(tmp_path / 'seed', {})
    for index, (fields_, magnitude) in enumerate(((dict(MEIJJAA, tpfd=None), 50.0),
                                                  ({'tbl': 2, 'toff': 5, 'hstrt': 4, 'hend': 4},
                                                   90.0))):
        seed.append({'id': str(index), 'kind': 'move', 'status': 'ok', **fields_,
                     'score': {'median_magnitude': magnitude}})
    stub_printer.magnitude = edgy
    run_collect(stub_printer, '--seed-from', str(seed.root))
    assert stub_printer.measured[0] == tmc.Chopper(1, 2, 2, 15)
    assert 'Seeded from %s: starting at tbl1_toff2_hstrt2_hend15' % seed.root \
        in capsys.readouterr().out


def test_klipper_defaults_outside_the_ranges_stay_out_of_the_rounds():
    from types import SimpleNamespace
    hw = SimpleNamespace(driver=DRIVER, baseline={'tbl': 3, 'toff': 4, 'hstrt': 1, 'hend': 2})
    rivals, why = collect.block_rivals(hw, macro('collect', 'SPEED=58', 'TBL=3'))
    assert rivals == [tmc.Chopper(3, 4, 1, 2)] and why == ''
    rivals, why = collect.block_rivals(hw, macro('collect', 'SPEED=58'))
    assert rivals == [DRIVER.default, tmc.Chopper(3, 4, 1, 2)]


def test_your_tpfd_outside_the_tpfd_range_stays_out_of_the_rounds():
    from types import SimpleNamespace
    driver = tmc.DRIVERS['5160']
    hw = SimpleNamespace(driver=driver, baseline={'tbl': 2, 'toff': 3, 'hstrt': 4, 'hend': 4,
                                                  'tpfd': 12})
    rivals, why = collect.block_rivals(hw, macro('tune', 'MOTOR=A', 'TPFD=0:7'))
    assert rivals == [tmc.stock_chopper(driver, True)] and why == 'TPFD 12 is outside TPFD=0:7'


@pytest.mark.parametrize('start, nearest', [
    # tavalin's: of the two as near, the lower HSTRT, so HEND stays
    (tmc.Chopper(0, 3, 3, 15), tmc.Chopper(1, 3, 2, 15)),
    (tmc.Chopper(2, 3, 5, 0), tmc.Chopper(2, 3, 5, 0)),          # inside: as it is
    (tmc.Chopper(0, 1, 4, 4), tmc.Chopper(1, 2, 4, 4)),          # TOFF 1 needs TBL 2 or more
    (tmc.Chopper(0, 3, 3, 15, 20), tmc.Chopper(1, 3, 2, 15, 15)),
    (tmc.Chopper(1, 3, 3, 15, None), tmc.Chopper(1, 3, 2, 15, None)),
])
def test_the_nearest_combo_inside_moves_each_register_the_least(start, nearest):
    assert tmc.SEARCH_SPACE.nearest(start, DRIVER) == nearest


def test_the_nearest_combo_keeps_to_the_driver():
    # Klipper refuses HSTRT + HEND above 15 on a TMC2660: HSTRT gives way, HEND stays
    assert tmc.SEARCH_SPACE.nearest(tmc.Chopper(2, 4, 7, 15), tmc.DRIVERS['2660']) \
        == tmc.Chopper(2, 4, 0, 15)
    assert tmc.Space(tmc.Range(0, 1), tmc.Range(1, 1), tmc.Range(0, 7), tmc.Range(0, 15)) \
        .nearest(tmc.Chopper(2, 3, 5, 0), DRIVER) is None


@pytest.mark.parametrize('argv, refusal', [
    (('--tbl', '0:1', '--toff', '1'),
     'widen ranges: no combination of TBL=0:1 TOFF=1 HSTRT=0:7 HEND=0:15 fits the TMC2209 '
     'limits'),
    # the driver takes 0/3/3/15: only the cap leaves it out
    (('--tbl', '0', '--toff', '3', '--hstrt', '3', '--hend', '15'),
     'set MAX_HYSTERESIS=16: the lowest hysteresis of TBL=0 TOFF=3 HSTRT=3 HEND=15 on TMC2209 '
     'is 16'),
])
def test_ranges_with_nothing_to_try_are_refused_before_anything_moves(stub_printer, argv,
                                                                       refusal):
    with pytest.raises(SystemExit) as refused:
        run_collect(stub_printer, *argv)
    assert refused.value.code == refusal
    assert stub_printer.log == []
    # the LCD row starts with what to do, KlipperScreen and Mainsail show the whole reason
    assert collect.failure_display(refusal) == 'FAIL ' + refusal


@pytest.mark.parametrize('params, refusal', [
    (['TBL=0:1', 'TOFF=1'], 'widen ranges'),
    # TOFF 8 alone runs every chopper below 30 kHz
    (['TOFF=8', 'SKIP_AUDIBLE=1'], 'SKIP_AUDIBLE leaves nothing to try'),
])
def test_a_tune_refuses_its_ranges_before_the_speed_scan(stub_printer, monkeypatch, params,
                                                         refusal):
    stub_printer.settings = {'tmc2209 stepper_x': {}, 'tmc2209 stepper_y': {}}
    monkeypatch.setattr(tune, 'scan', lambda *args, **kwargs: pytest.fail('the scan ran'))
    with pytest.raises(SystemExit, match=refusal):
        tune.run_tune(macro('tune', 'MOTOR=A', *params))
    assert stub_printer.log == []


def test_the_extruder_refuses_its_ranges_before_heating(monkeypatch):
    heated = []
    monkeypatch.setattr(extruder_mod, 'heat_script', lambda temp: heated.append(temp))
    with pytest.raises(SystemExit, match='widen ranges'):
        run_extruder(monkeypatch, 'TBL=0:1', 'TOFF=1')
    assert heated == []


@pytest.mark.parametrize('rounds', [(), ('--validate', '0')])
def test_a_resume_under_other_ranges_recommends_from_its_own(stub_printer, tmp_path, rounds):
    stub_printer.magnitude = edgy
    root = str(tmp_path / 'resumed')
    first = run_collect(stub_printer, '--dataset', root, *rounds, *EDGES)
    assert on_edge(tmc.Chopper(**first['winner']))
    before = len(stub_printer.measured)
    second = run_collect(stub_printer, '--dataset', root, *rounds)
    assert not on_edge(tmc.Chopper(**second['winner']))
    assert not any(on_edge(c) for c in stub_printer.measured[before:])
    assert tmc.Space.recorded(second) == tmc.Space.of(macro('collect', 'SPEED=58'))


def test_a_stopped_resume_under_other_ranges_forgets_the_old_winner_and_its_edges(
        stub_printer, monkeypatch, tmp_path, capsys):
    stub_printer.magnitude = edgy
    root = str(tmp_path / 'stopped')
    run_collect(stub_printer, '--dataset', root, *EDGES)

    def stop(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(collect, 'measure_baseline', stop)
    with pytest.raises(KeyboardInterrupt):
        run_collect(stub_printer, '--dataset', root)
    manifest = Dataset.open(root).manifest()
    assert manifest['winner'] is None and manifest['max_hysteresis'] == 15
    # without a winner CHOPPER_SAVE and CHOPPER_ANALYZE rank the dataset, edges of the old
    # run included, inside the ranges of the new one
    saved = tune.winner_of(root)[1]
    assert not on_edge(saved)
    capsys.readouterr()
    run_analyze(build_parser().parse_args(['analyze', root, '--no-html']))
    out = capsys.readouterr().out
    assert tmc.cfg_snippet(DRIVER, 'stepper_x', saved) in out
    assert ('measured combos are left out: they are outside the ranges of the last run on '
            'this dataset, TBL=1:3 TOFF=1:8 HSTRT=0:7 HEND=0:15 MAX_HYSTERESIS=15') in out


# 5. SPEED of TUNE and DEMO is one speed, and a tune of both motors takes none

@pytest.mark.parametrize('command', ['tune', 'demo'])
@pytest.mark.parametrize('value', ['40:70', '40.5', '0'])
def test_speed_is_one_speed(command, value, capsys):
    with pytest.raises(SystemExit):
        macro(command, 'MOTOR=A', 'SPEED=' + value)
    assert ("argument --speed: expected one speed as a whole number of mm/s above 0, e.g. 40, "
            "got '%s'" % value) in capsys.readouterr().err
    assert macro(command, 'MOTOR=A', 'SPEED=40').speed == 40


@pytest.mark.parametrize('params, command', [
    (['SPEED=40'], 'CHOPPER_TUNE MOTOR=A SPEED=40'),
    (['SPEED=250'], 'CHOPPER_TUNE MOTOR=A SPEED=250'),
    (['SPEED=40', 'SAVE=1', 'TBL=0:3'], 'CHOPPER_TUNE MOTOR=A SPEED=40 SAVE=1 TBL=0:3'),
])
def test_a_speed_for_both_motors_is_refused_before_anything_starts(monkeypatch, params, command):
    monkeypatch.setattr(tune, 'Klippy', lambda sock: pytest.fail('connected to Klipper'))
    with pytest.raises(SystemExit) as refusal:
        tune.run_tune(macro('tune', *params))
    assert refusal.value.code == ('add MOTOR=A or B: each motor has its own resonance speed. '
                                  'For example: ' + command)
    # the KlipperScreen status line and the Mainsail header show the command whole
    assert collect.failure_display(refusal.value.code).endswith(command)


def test_a_speed_for_one_motor_tunes_it_there(stub_printer, monkeypatch):
    speeds = []
    monkeypatch.setattr(tune, 'scan', lambda *args, **kwargs: pytest.fail('the scan ran'))
    monkeypatch.setattr(tune, 'collect', lambda kl, args, popup: speeds.append(args.speed) or (0, None))
    tune.run_tune(macro('tune', 'MOTOR=B', 'SPEED=62'))
    assert speeds == [tmc.Range(62, 62)]


# 6. the plan of a dry run shows the ranges and the cap in force

def test_a_dry_register_search_shows_its_ranges(stub_printer, capsys):
    def dry(*argv):
        assert collect.collect(stub_printer.kl(), build_parser().parse_args(
            ['collect', '--speed', '58', '--dry-run', *argv])) == (0, None)
    dry()
    assert 'Registers the run tries: TBL=1:3 TOFF=1:8 HSTRT=0:7 HEND=0:15 MAX_HYSTERESIS=15' \
        in capsys.readouterr().out.splitlines()
    dry('--search', 'descent', '--toff', '3:5', *EDGES)
    assert 'Registers the run tries: TBL=0:3 TOFF=3:5 HSTRT=0:7 HEND=0:15 MAX_HYSTERESIS=16' \
        in capsys.readouterr().out.splitlines()
    assert stub_printer.log == []


def test_a_dry_tune_shows_its_ranges(stub_printer, monkeypatch, capsys):
    run_tune('--dry-run')               # a TMC2209 has no TPFD to sweep
    assert 'Registers the run tries: TBL=1:3 TOFF=1:8 HSTRT=0:7 HEND=0:15 MAX_HYSTERESIS=15' \
        in capsys.readouterr().out.splitlines()
    with_motor(stub_printer, monkeypatch, '5160', {})
    run_tune('--dry-run')
    assert ('Registers the run tries: TBL=1:3 TOFF=1:8 HSTRT=0:7 HEND=0:15 TPFD=0:15 '
            'MAX_HYSTERESIS=15') in capsys.readouterr().out.splitlines()
    run_tune('--dry-run', '--hend', '4:12', *EDGES)
    assert ('Registers the run tries: TBL=0:3 TOFF=1:8 HSTRT=0:7 HEND=4:12 TPFD=0:15 '
            'MAX_HYSTERESIS=16') in capsys.readouterr().out.splitlines()


def test_a_dry_extruder_tune_shows_its_ranges(capsys):
    from types import SimpleNamespace
    kl = SimpleNamespace(settings=lambda: {'tmc2209 extruder': {}, 'extruder': {}})
    for params, ranges in (([], 'TBL=1:3 TOFF=1:8 HSTRT=0:7 HEND=0:15 MAX_HYSTERESIS=15'),
                           (['TBL=0:3', 'MAX_HYSTERESIS=16'],
                            'TBL=0:3 TOFF=1:8 HSTRT=0:7 HEND=0:15 MAX_HYSTERESIS=16')):
        assert extruder_mod.extruder_tune(kl, macro('extruder', 'DRY_RUN=1', *params)) == 0
        assert 'Registers the run tries: ' + ranges in capsys.readouterr().out.splitlines()
