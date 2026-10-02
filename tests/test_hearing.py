"""How high a chopper must run to be inaudible (#157). The limit applies to the estimate,
an upper bound of the real frequency, so the owner of the ears raises it; SKIP_AUDIBLE
keeps a combo below it out of the run and out of the ranking."""
import argparse

import pytest

import chopper_autotune.collect as collect_module
from chopper_autotune import tmc, tune
from chopper_autotune.analyze import rank, run_analyze, tbl_toff_matrix
from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
from chopper_autotune.collect import Range, build_plan, collect, refuse_unhearable
from chopper_autotune.dataset import Dataset
from chopper_autotune.search import penalized_score, seed_start

DRIVER = tmc.DRIVERS['2209']

# the motor A table of #157 (TMC2209, 17HS16-2004S1): 3/7/0/0 at 20.8 kHz whined,
# 3/5/0/0 at 26.8 kHz still did, 3/4/0/0 at 31.3 kHz was quiet
REPORTED = [((3, 7, 0, 0), 6614.7), ((3, 5, 0, 0), 7373.6), ((3, 6, 0, 0), 7333.5),
            ((3, 7, 1, 0), 7609.4), ((3, 7, 1, 1), 7730.3), ((2, 8, 0, 0), 7971.7),
            ((3, 7, 0, 2), 8087.6), ((3, 8, 0, 0), 6771.8), ((3, 7, 0, 1), 8215.2),
            ((3, 4, 0, 0), 8619.8)]
QUIET = tmc.Hearing(limit_hz=31000, skip=True)


def reported() -> 'list[dict]':
    return [{'chopper': tmc.Chopper(*fields), 'magnitude': magnitude, 'spread': 0.0, 'n': 2}
            for fields, magnitude in REPORTED]


def reported_dataset(tmp_path, hearing: tmc.Hearing = QUIET, winner=None) -> Dataset:
    """The #157 table as a run with this hearing that recorded this winner."""
    manifest = {'driver': '2209', 'stepper': 'stepper_x', 'trim': 0.1,
                **hearing.manifest_fields()}
    if winner:
        manifest['winner'] = winner.fields()
    ds = Dataset.create(tmp_path / 'd', manifest)
    for index, (fields, magnitude) in enumerate(REPORTED):
        ds.append({'id': str(index), 'kind': 'move', 'status': 'ok',
                   **tmc.Chopper(*fields).fields(), 'score': {'median_magnitude': magnitude}})
    return ds


def macro(*params: str) -> argparse.Namespace:
    parser = build_parser()
    return parser.parse_args(_gcode_args(list(params), boolean_flags(parser)))


def test_the_estimate_follows_the_datasheets():
    # slow decay 24 + 32 * TOFF clocks on the TMC2209, 12 + 32 * TOFF on the TMC2660
    assert tmc.chopper_freq_hz(tmc.Chopper(3, 4, 0, 0), DRIVER) == pytest.approx(12e6 / (2 * (40 + 24 + 128)))
    assert tmc.chopper_freq_hz(tmc.Chopper(2, 4, 0, 0), tmc.DRIVERS['2660']) \
        == pytest.approx(15e6 / (2 * (36 + 12 + 128)))


def test_the_default_limit_picks_the_combo_that_whined():
    assert rank(reported(), DRIVER, tmc.Hearing())[0]['chopper'] == tmc.Chopper(3, 7, 0, 0)


def test_a_raised_limit_alone_is_a_penalty_the_quiet_combo_30_percent_louder_loses():
    ranked = rank(reported(), DRIVER, tmc.Hearing(limit_hz=31000))
    assert ranked[0]['chopper'] == tmc.Chopper(3, 7, 0, 0) and ranked[0]['audible']


def test_skip_audible_with_a_raised_limit_ranks_only_what_stays_inaudible():
    assert [a['chopper'] for a in rank(reported(), DRIVER, QUIET)] == [tmc.Chopper(3, 4, 0, 0)]


def test_the_grid_plans_only_combos_at_or_above_the_limit():
    plan = build_plan(DRIVER, Range(0, 3), Range(1, 8), Range(0, 0), Range(0, 0), None, [58], QUIET)
    assert {combo.toff for combo, _ in plan} == {1, 2, 3, 4}
    assert all(tmc.chopper_freq_hz(combo, DRIVER) >= 31000 for combo, _ in plan)


def test_a_limit_no_combo_of_the_ranges_reaches_is_refused():
    unreachable = tmc.Hearing(limit_hz=70000, skip=True)
    with pytest.raises(SystemExit, match=r'runs at 68.2 kHz, below AUDIBLE_KHZ=70. Lower AUDIBLE_KHZ$'):
        refuse_unhearable(DRIVER, Range(0, 3), Range(1, 8), unreachable)
    with pytest.raises(SystemExit, match='Lower AUDIBLE_KHZ or widen TBL/TOFF'):
        refuse_unhearable(DRIVER, Range(0, 3), Range(5, 8), QUIET, widen=True)
    refuse_unhearable(DRIVER, Range(0, 3), Range(1, 8), tmc.Hearing(limit_hz=68000, skip=True))
    refuse_unhearable(DRIVER, Range(0, 3), Range(1, 8), tmc.Hearing(limit_hz=70000))


def test_the_caution_band_starts_at_the_limit():
    quiet = tmc.Chopper(3, 4, 0, 0)                 # 31.3 kHz
    assert tmc.edge_penalty(quiet, DRIVER, tmc.Hearing(limit_hz=20000)) == 0
    assert tmc.edge_penalty(quiet, DRIVER, tmc.Hearing()) > 0


def test_the_command_line_hears_first_then_the_dataset_then_the_defaults():
    def line(khz=None, weight=None, skip=None):
        return argparse.Namespace(audible_khz=khz, audible_weight=weight, skip_audible=skip)
    recorded = tmc.Hearing(31000, 1.0, True).manifest_fields()
    assert tmc.Hearing.of(line(), recorded) == tmc.Hearing(31000, 1.0, True)
    assert tmc.Hearing.of(line(khz=25.0, skip=False), recorded) == tmc.Hearing(25000, 1.0, False)
    assert tmc.Hearing.of(line(weight=0.5, skip=True)) == tmc.Hearing(30000, 0.5, True)
    assert tmc.Hearing.of(line()) == tmc.Hearing()
    # a dataset from before the limit was recorded was heard at 20 kHz; its skip went by the
    # estimate of the day, so it is not applied again by this one
    assert tmc.Hearing.of(line(), {'audible_weight': 0.25, 'skip_audible': True}) \
        == tmc.Hearing(20000, 0.25, False)


def test_the_default_limit_penalizes_the_whine_of_157():
    # 30 kHz of the estimate: 3/5/0/0 (26.8 kHz, a whine) is penalized, 3/4/0/0 (31.3) not
    hearing = tmc.Hearing()
    assert hearing.audible(tmc.Chopper(3, 5, 0, 0), DRIVER)
    assert not hearing.audible(tmc.Chopper(3, 4, 0, 0), DRIVER)


@pytest.mark.parametrize('flag, value', [('--audible-khz', '0'), ('--audible-khz', '-5'),
                                         ('--audible-khz', 'nan'), ('--audible-khz', 'inf'),
                                         ('--audible-khz', 'loud'), ('--audible-weight', '-0.1'),
                                         ('--audible-weight', 'nan'), ('--audible-weight', 'inf')])
def test_the_limit_is_a_frequency_and_the_weight_a_penalty(flag, value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(['analyze', flag, value])


def test_skip_audible_0_lifts_the_skip_a_dataset_recorded(tmp_path, capsys):
    parser = build_parser()
    assert _gcode_args(['analyze', 'SKIP_AUDIBLE=0', 'DRY_RUN=0'], boolean_flags(parser)) \
        == ['analyze', '--no-skip-audible']
    run_analyze(macro('analyze', str(reported_dataset(tmp_path).root), 'NO_HTML=1',
                      'SKIP_AUDIBLE=0'))
    out = capsys.readouterr().out
    assert 'left out' not in out and 'driver_TOFF: 7' in out


def test_tune_hands_the_macro_hearing_to_the_register_search():
    search = tune.collect_args(macro('tune', 'AUDIBLE_KHZ=31', 'SKIP_AUDIBLE=1'), 'x',
                               Range(58, 58), None)
    assert tmc.Hearing.of(search) == tmc.Hearing(31000, tmc.AUDIBLE_WEIGHT, True)
    plain = tune.collect_args(build_parser().parse_args(['tune']), 'x', Range(58, 58), None)
    assert tmc.Hearing.of(plain) == tmc.Hearing()


def test_tune_refuses_an_unreachable_limit_before_the_speed_scan(stub_printer, monkeypatch):
    stub_printer.settings = {'tmc2209 stepper_x': {}, 'tmc2209 stepper_y': {}}
    monkeypatch.setattr(tune, 'scan', lambda *args, **kwargs: pytest.fail('the scan ran'))
    with pytest.raises(SystemExit, match='AUDIBLE_KHZ=90. Lower AUDIBLE_KHZ$'):
        tune.run_tune(build_parser().parse_args(['tune', '--audible-khz', '90', '--skip-audible']))
    assert 'MOVE' not in stub_printer.log


def test_a_tune_that_skips_the_audible_measures_and_picks_only_the_inaudible(stub_printer):
    # the stub's quietest chopper, toff 5, runs at 26.8-30.0 kHz: under the default it wins
    tune.run_tune(build_parser().parse_args(['tune', '--motor', 'a', '--speed', '58', '--no-raw',
                                             '--audible-khz', '31', '--skip-audible']))
    assert stub_printer.measured
    assert all(tmc.chopper_freq_hz(combo, DRIVER) >= 31000 for combo in stub_printer.measured)
    manifest = Dataset.open(stub_printer.roots[-1]).manifest()
    winner = tmc.Chopper(**manifest['winner'])
    assert winner.toff == 4 and tmc.chopper_freq_hz(winner, DRIVER) >= 31000
    assert tmc.Hearing.of(recorded=manifest) == tmc.Hearing(31000, tmc.AUDIBLE_WEIGHT, True)


def test_skip_audible_does_not_play_even_the_stock_reference(stub_printer):
    # stock 2/3/5/0 runs at 39.5 kHz: below a 45 kHz limit it is never measured, and the
    # run makes no claim against it
    tune.run_tune(build_parser().parse_args(['tune', '--motor', 'a', '--speed', '58', '--no-raw',
                                             '--audible-khz', '45', '--skip-audible']))
    assert stub_printer.measured
    assert all(tmc.chopper_freq_hz(combo, DRIVER) >= 45000 for combo in stub_printer.measured)
    assert Dataset.open(stub_printer.roots[-1]).manifest().get('improvement') is None


def collect_into(printer, root, *hearing: str):
    collect(printer.kl(), build_parser().parse_args(
        ['collect', '--axis', 'x', '--speed', '58', '--search', 'descent', '--yes', '--no-raw',
         '--dataset', str(root), *hearing]))
    return Dataset.open(root).manifest()


def test_a_resumed_run_that_now_skips_the_audible_never_picks_one_it_measured(stub_printer,
                                                                             tmp_path):
    first = collect_into(stub_printer, tmp_path / 'resumed')
    assert tmc.chopper_freq_hz(tmc.Chopper(**first['winner']), DRIVER) < 31000
    before = len(stub_printer.measured)
    second = collect_into(stub_printer, tmp_path / 'resumed', '--audible-khz', '31', '--skip-audible')
    assert tmc.chopper_freq_hz(tmc.Chopper(**second['winner']), DRIVER) >= 31000
    # the combos the first run measured below the limit are not even re-measured
    assert all(tmc.chopper_freq_hz(combo, DRIVER) >= 31000
               for combo in stub_printer.measured[before:])
    # what a later ANALYZE or SAVE re-ranks with: the hearing the winner was picked with
    assert tmc.Hearing.of(recorded=second) == tmc.Hearing(31000, tmc.AUDIBLE_WEIGHT, True)


def test_a_resume_with_another_hearing_forgets_the_old_winner(stub_printer, tmp_path,
                                                             monkeypatch):
    collect_into(stub_printer, tmp_path / 'stopped')

    def stop(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(collect_module, 'measure_baseline', stop)
    with pytest.raises(KeyboardInterrupt):
        collect_into(stub_printer, tmp_path / 'stopped', '--audible-khz', '31', '--skip-audible')
    manifest = Dataset.open(tmp_path / 'stopped').manifest()
    assert manifest['winner'] is None and manifest['improvement'] is None
    assert tmc.Hearing.of(recorded=manifest) == tmc.Hearing(31000, tmc.AUDIBLE_WEIGHT, True)


def test_the_landscape_marks_what_the_hearing_calls_audible():
    hearing = tmc.Hearing(limit_hz=31000)
    tbls, toffs, _, text = tbl_toff_matrix(rank(reported(), DRIVER, hearing), DRIVER, hearing)
    cell = {(tbl, toff): text[row][column] for row, tbl in enumerate(tbls)
            for column, toff in enumerate(toffs)}
    assert cell[(3, 7)].endswith('!') and cell[(3, 5)].endswith('!')
    assert not cell[(3, 4)].endswith('!')


def test_a_collect_with_an_unreachable_limit_moves_nothing(stub_printer, tmp_path):
    with pytest.raises(SystemExit, match='Lower AUDIBLE_KHZ or widen TBL/TOFF'):
        collect_into(stub_printer, tmp_path / 'never', '--audible-khz', '90', '--skip-audible')
    assert 'MOVE' not in stub_printer.log


def test_a_skipped_combo_scores_infinite_and_never_seeds_a_search(tmp_path):
    assert penalized_score(tmc.Chopper(3, 7, 0, 0), [6614.7], DRIVER, QUIET) == float('inf')
    ds = reported_dataset(tmp_path)
    assert seed_start(ds, DRIVER, tmc.Hearing()) == tmc.Chopper(3, 7, 0, 0)
    assert seed_start(ds, DRIVER, QUIET) == tmc.Chopper(3, 4, 0, 0)


def test_save_re_ranks_a_dataset_as_its_run_heard(tmp_path):
    _, winner = tune.winner_of(str(reported_dataset(tmp_path).root))
    assert winner == tmc.Chopper(3, 4, 0, 0)


def test_analyze_hears_as_the_run_did_unless_told_otherwise(tmp_path, capsys):
    ds = reported_dataset(tmp_path)
    run_analyze(build_parser().parse_args(['analyze', str(ds.root), '--no-html']))
    out = capsys.readouterr().out
    assert '9 measured combos are left out: their chopper runs below 31 kHz' in out
    assert 'driver_TOFF: 4' in out
    run_analyze(build_parser().parse_args(['analyze', str(ds.root), '--no-html',
                                           '--audible-khz', '20']))
    assert 'driver_TOFF: 7' in capsys.readouterr().out


def test_a_recorded_winner_the_hearing_skips_is_neither_recommended_nor_saved(tmp_path, capsys):
    # a default TUNE recorded the whining 3/7/0/0; ANALYZE or SAVE with the user's limit
    # must not hand it back
    ds = reported_dataset(tmp_path, tmc.Hearing(), winner=tmc.Chopper(3, 7, 0, 0))
    told = ['--audible-khz', '31', '--skip-audible']
    run_analyze(build_parser().parse_args(['analyze', str(ds.root), '--no-html', *told]))
    out = capsys.readouterr().out
    assert 'tbl3_toff7_hstrt0_hend0, runs below 31 kHz (SKIP_AUDIBLE)' in out
    assert 'driver_TOFF: 4' in out
    _, winner = tune.winner_of(str(ds.root), build_parser().parse_args(['save', *told]))
    assert winner == tmc.Chopper(3, 4, 0, 0)
    assert tune.winner_of(str(ds.root))[1] == tmc.Chopper(3, 7, 0, 0)


def save_latest(monkeypatch, tmp_path, argv, extruder_state=None):
    """CHOPPER_SAVE over the datasets in tmp_path; what it hands run_save, or None."""
    from types import SimpleNamespace

    from chopper_autotune import analyze
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: sorted(tmp_path.iterdir()))
    monkeypatch.setattr('chopper_autotune.extruder.load_winner_state', lambda: extruder_state)
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: SimpleNamespace(settings=lambda: {}))
    saved = {}
    monkeypatch.setattr(analyze, 'run_save', lambda mk, items, extruder_state=None: saved.update(
        items=items, extruder=extruder_state))
    analyze.run_save_latest(build_parser().parse_args(['save', *argv]))
    return saved


def motor_dataset(root, winner: tmc.Chopper, measured: 'list[tmc.Chopper]'):
    ds = Dataset.create(root, {'axis': 'x', 'search': 'descent', 'driver': '2209',
                               'stepper': 'stepper_x', 'trim': 0.1, 'winner': winner.fields()})
    for index, combo in enumerate(measured):
        ds.append({'id': str(index), 'kind': 'move', 'status': 'ok', **combo.fields(),
                   'score': {'median_magnitude': 1000.0}})


def test_save_never_falls_back_to_an_older_run_when_the_hearing_skips_the_newest(
        monkeypatch, tmp_path, capsys):
    # the newest run measured only below 31 kHz: an older run is no answer to that
    motor_dataset(tmp_path / '01_old', tmc.Chopper(0, 2, 4, 7), [tmc.Chopper(0, 2, 4, 7)])
    motor_dataset(tmp_path / '02_new', tmc.Chopper(3, 5, 0, 0),
                  [tmc.Chopper(3, toff, 0, 0) for toff in range(5, 9)])
    with pytest.raises(SystemExit, match=r'^motor A: re-run CHOPPER_TUNE with AUDIBLE_KHZ=31: '
                                         r'SKIP_AUDIBLE skips all of 02_new$'):
        save_latest(monkeypatch, tmp_path, ['--audible-khz', '31', '--skip-audible'])
    out = capsys.readouterr().out
    assert 'NOT saving 02_new' in out and '01_old' not in out


def test_save_and_save_last_keep_an_extruder_winner_the_hearing_skips(monkeypatch, tmp_path):
    from chopper_autotune import extruder
    whining = {'driver': '2209', 'fields': {'tbl': 3, 'toff': 7, 'hstrt': 0, 'hend': 0},
               **tmc.Hearing().manifest_fields()}
    assert save_latest(monkeypatch, tmp_path, [], whining)['extruder'] == whining
    with pytest.raises(SystemExit, match=r'runs below 30 kHz \(SKIP_AUDIBLE\)'):
        save_latest(monkeypatch, tmp_path, ['--skip-audible'], whining)
    monkeypatch.setattr(extruder, 'load_winner_state', lambda: whining)
    with pytest.raises(SystemExit, match=r'stored extruder winner .* runs below 30 kHz'):
        extruder.extruder_tune(None, build_parser().parse_args(['extruder', '--save-last',
                                                                '--skip-audible']))


def test_a_run_from_before_keeps_its_recorded_winner(tmp_path, capsys):
    # an old SKIP_AUDIBLE=1 run skipped by 12 + 32 * TOFF: its winner 2/8/4/4 ran at 20.0 kHz
    # then, 19.2 kHz by the datasheets now; neither SAVE nor ANALYZE drops it after the fact
    ds = Dataset.create(tmp_path / 'old', {'axis': 'x', 'search': 'descent', 'driver': '2209',
                                           'stepper': 'stepper_x', 'trim': 0.1,
                                           'audible_weight': 0.25, 'skip_audible': True,
                                           'winner': tmc.Chopper(2, 8, 4, 4).fields()})
    for index, (combo, magnitude) in enumerate([(tmc.Chopper(2, 8, 4, 4), 900.0),
                                                (tmc.Chopper(2, 6, 4, 4), 950.0)]):
        ds.append({'id': str(index), 'kind': 'move', 'status': 'ok', **combo.fields(),
                   'score': {'median_magnitude': magnitude}})
    assert tune.winner_of(str(ds.root), build_parser().parse_args(['save']))[1] \
        == tmc.Chopper(2, 8, 4, 4)
    run_analyze(build_parser().parse_args(['analyze', str(ds.root), '--no-html']))
    out = capsys.readouterr().out
    assert 'left out' not in out and 'driver_TOFF: 8' in out


def test_save_says_when_the_hearing_replaces_a_recorded_winner(tmp_path, capsys):
    ds = reported_dataset(tmp_path, tmc.Hearing(), winner=tmc.Chopper(3, 7, 0, 0))
    tune.winner_of(str(ds.root), build_parser().parse_args(['save', '--audible-khz', '31',
                                                            '--skip-audible']))
    assert ('The winner recorded in d, tbl3_toff7_hstrt0_hend0, runs below 31 kHz '
            '(SKIP_AUDIBLE)') in capsys.readouterr().out


def test_an_extruder_winner_hears_as_its_tune_did(monkeypatch, tmp_path):
    fields = {'tbl': 3, 'toff': 7, 'hstrt': 0, 'hend': 0}                 # 20.8 kHz
    before = {'driver': '2209', 'fields': fields}                        # heard at 20 kHz
    assert save_latest(monkeypatch, tmp_path, ['--skip-audible'], before)['extruder'] == before
    tuned = dict(before, **tmc.Hearing(31000).manifest_fields())
    with pytest.raises(SystemExit, match=r'runs below 31 kHz \(SKIP_AUDIBLE\)'):
        save_latest(monkeypatch, tmp_path, ['--skip-audible'], tuned)


def test_compare_says_when_the_two_sides_hear_differently(tmp_path, capsys):
    from chopper_autotune.analyze import run_compare
    roots = []
    for name, hearing in (('a', tmc.Hearing(20000)), ('b', tmc.Hearing())):
        ds = Dataset.create(tmp_path / name, {'driver': '2209', 'stepper': 'stepper_x',
                                              **hearing.manifest_fields()})
        for index, (fields, magnitude) in enumerate(REPORTED):
            ds.append({'id': str(index), 'kind': 'move', 'status': 'ok',
                       **tmc.Chopper(*fields).fields(), 'score': {'median_magnitude': magnitude}})
        roots.append(str(ds.root))
    run_compare(build_parser().parse_args(['compare', *roots]))
    assert 'ranked under different hearing (A 20 kHz, weight 0.25, B 30 kHz, weight 0.25)' \
        in capsys.readouterr().out
    run_compare(build_parser().parse_args(['compare', *roots, '--audible-khz', '30']))
    assert 'different hearing' not in capsys.readouterr().out


def test_the_extruder_tune_remembers_its_hearing_and_save_last_uses_it(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from chopper_autotune import extruder
    monkeypatch.setattr(extruder, 'STATE', str(tmp_path / 'extruder.json'))
    kl = SimpleNamespace(settings=lambda: {'tmc2209 extruder': {}, 'extruder': {}},
                         gcode=lambda script: None, gcode_output=lambda script: [],
                         subscribe_accel=lambda chip: None)
    monkeypatch.setattr(extruder, 'detect_hardware', lambda kl, axis: SimpleNamespace(
        accel_chip='adxl345', display=True))
    monkeypatch.setattr(extruder, 'refuse_if_printing', lambda kl: None)
    monkeypatch.setattr(extruder, 'Screen', lambda kl, display: SimpleNamespace(
        update=lambda *a, **k: None, final=lambda *a: None))
    monkeypatch.setattr(extruder, 'measure', lambda hw, speed: (100.0, 0))
    edge = tmc.Chopper(0, 5, 0, 0)                  # 30.0 kHz: heard under 31, not under 30
    monkeypatch.setattr(extruder, 'descent', lambda *args, **kwargs: (edge, {edge: 100.0}))
    extruder.extruder_tune(kl, build_parser().parse_args(
        ['extruder', '--speed', '5', '--audible-khz', '31', '--yes']))
    assert tmc.Hearing.of(recorded=extruder.load_winner_state()) == tmc.Hearing(31000)
    # SAVE_LAST hears as the tune did, at 31 kHz, not at the default 30
    monkeypatch.setattr('chopper_autotune.analyze._persist', lambda *args: pytest.fail('saved'))
    with pytest.raises(SystemExit, match=r'runs below 31 kHz \(SKIP_AUDIBLE\)'):
        extruder.extruder_tune(kl, build_parser().parse_args(['extruder', '--save-last',
                                                              '--skip-audible']))
