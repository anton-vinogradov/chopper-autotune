"""How high a chopper must run to be inaudible (#157). The limit applies to the estimate,
which runs above the real frequency, so the owner of the ears raises it; SKIP_AUDIBLE
keeps a combo below it out of the run and out of the ranking."""
import argparse

import pytest

from chopper_autotune import tmc, tune
from chopper_autotune.analyze import rank, run_analyze, tbl_toff_matrix
from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
from chopper_autotune.collect import Range, build_plan, collect, refuse_unhearable
from chopper_autotune.dataset import Dataset
from chopper_autotune.search import penalized_score, seed_start

DRIVER = tmc.DRIVERS['2209']

# the motor A table of #157 (TMC2209, 17HS16-2004S1): 3/7/0/0 at 21.7 kHz whined,
# 3/5/0/0 at 28.3 kHz still did, 3/4/0/0 at 33.3 kHz was quiet
REPORTED = [((3, 7, 0, 0), 6614.7), ((3, 5, 0, 0), 7373.6), ((3, 6, 0, 0), 7333.5),
            ((3, 7, 1, 0), 7609.4), ((3, 7, 1, 1), 7730.3), ((2, 8, 0, 0), 7971.7),
            ((3, 7, 0, 2), 8087.6), ((3, 8, 0, 0), 6771.8), ((3, 7, 0, 1), 8215.2),
            ((3, 4, 0, 0), 8619.8)]


def reported() -> 'list[dict]':
    return [{'chopper': tmc.Chopper(*fields), 'magnitude': magnitude, 'spread': 0.0, 'n': 2}
            for fields, magnitude in REPORTED]


def test_the_default_limit_picks_the_combo_that_whined():
    assert rank(reported(), DRIVER, tmc.Hearing())[0]['chopper'] == tmc.Chopper(3, 7, 0, 0)


def test_a_raised_limit_alone_is_a_penalty_the_quiet_combo_30_percent_louder_loses():
    ranked = rank(reported(), DRIVER, tmc.Hearing(limit_hz=33000))
    assert ranked[0]['chopper'] == tmc.Chopper(3, 7, 0, 0) and ranked[0]['audible']


def test_skip_audible_with_a_raised_limit_ranks_only_what_stays_inaudible():
    ranked = rank(reported(), DRIVER, tmc.Hearing(limit_hz=33000, skip=True))
    assert [a['chopper'] for a in ranked] == [tmc.Chopper(3, 4, 0, 0)]


def test_the_grid_plans_only_combos_at_or_above_the_limit():
    plan = build_plan(DRIVER, Range(0, 3), Range(1, 8), Range(0, 0), Range(0, 0), None, [58],
                      tmc.Hearing(limit_hz=33000, skip=True))
    assert {combo.toff for combo, _ in plan} == {1, 2, 3, 4}
    assert all(tmc.chopper_freq_hz(combo, DRIVER) >= 33000 for combo, _ in plan)


def test_a_limit_no_combo_of_the_ranges_reaches_is_refused():
    with pytest.raises(SystemExit, match='runs at 78.9 kHz, below AUDIBLE_KHZ=80'):
        refuse_unhearable(DRIVER, Range(0, 3), Range(1, 8), tmc.Hearing(limit_hz=80000, skip=True))
    refuse_unhearable(DRIVER, Range(0, 3), Range(1, 8), tmc.Hearing(limit_hz=78000, skip=True))
    refuse_unhearable(DRIVER, Range(0, 3), Range(1, 8), tmc.Hearing(limit_hz=80000))


def test_the_caution_band_starts_at_the_limit():
    quiet = tmc.Chopper(3, 4, 0, 0)                 # 33.3 kHz
    assert tmc.edge_penalty(quiet, DRIVER, tmc.Hearing()) == 0
    assert tmc.edge_penalty(quiet, DRIVER, tmc.Hearing(limit_hz=30000)) > 0


def test_the_command_line_hears_first_then_the_dataset_then_the_defaults():
    def line(khz=None, weight=None, skip=False):
        return argparse.Namespace(audible_khz=khz, audible_weight=weight, skip_audible=skip)
    recorded = tmc.Hearing(33000, 1.0, True).manifest_fields()
    assert tmc.Hearing.of(line(), recorded) == tmc.Hearing(33000, 1.0, True)
    assert tmc.Hearing.of(line(khz=25.0), recorded) == tmc.Hearing(25000, 1.0, True)
    assert tmc.Hearing.of(line(weight=0.5, skip=True)) == tmc.Hearing(20000, 0.5, True)
    assert tmc.Hearing.of(line()) == tmc.Hearing()


@pytest.mark.parametrize('value', ['0', '-5', 'nan', 'inf', 'loud'])
def test_the_limit_is_a_frequency(value):
    with pytest.raises(SystemExit):
        build_parser().parse_args(['analyze', '--audible-khz', value])


def test_tune_hands_the_macro_hearing_to_the_register_search():
    parser = build_parser()
    args = parser.parse_args(_gcode_args(['tune', 'AUDIBLE_KHZ=33', 'SKIP_AUDIBLE=1'],
                                         boolean_flags(parser)))
    search = tune.collect_args(args, 'x', Range(58, 58), None)
    assert tmc.Hearing.of(search) == tmc.Hearing(33000, tmc.AUDIBLE_WEIGHT, True)
    plain = tune.collect_args(parser.parse_args(['tune']), 'x', Range(58, 58), None)
    assert tmc.Hearing.of(plain) == tmc.Hearing()


def test_tune_refuses_an_unreachable_limit_before_the_speed_scan(stub_printer, monkeypatch):
    stub_printer.settings = {'tmc2209 stepper_x': {}, 'tmc2209 stepper_y': {}}
    monkeypatch.setattr(tune, 'scan', lambda *args, **kwargs: pytest.fail('the scan ran'))
    with pytest.raises(SystemExit, match='AUDIBLE_KHZ=90'):
        tune.run_tune(build_parser().parse_args(['tune', '--audible-khz', '90', '--skip-audible']))
    assert 'MOVE' not in stub_printer.log


def test_a_tune_that_skips_the_audible_measures_and_picks_only_the_inaudible(stub_printer):
    # the stub's quietest chopper, toff 5, runs at 28.3-31.9 kHz: under the default it wins
    tune.run_tune(build_parser().parse_args(['tune', '--motor', 'a', '--speed', '58', '--no-raw',
                                             '--audible-khz', '33', '--skip-audible']))
    assert stub_printer.measured
    assert all(tmc.chopper_freq_hz(combo, DRIVER) >= 33000 for combo in stub_printer.measured)
    manifest = Dataset.open(stub_printer.roots[-1]).manifest()
    winner = tmc.Chopper(**manifest['winner'])
    assert winner.toff == 4 and tmc.chopper_freq_hz(winner, DRIVER) >= 33000
    assert tmc.Hearing.of(recorded=manifest) == tmc.Hearing(33000, tmc.AUDIBLE_WEIGHT, True)


def test_a_resumed_run_that_now_skips_the_audible_never_picks_one_it_measured(stub_printer,
                                                                             tmp_path):
    def run(*hearing):
        collect(stub_printer.kl(), build_parser().parse_args(
            ['collect', '--axis', 'x', '--speed', '58', '--search', 'descent', '--yes',
             '--no-raw', '--dataset', str(tmp_path / 'resumed'), *hearing]))
        return tmc.Chopper(**Dataset.open(tmp_path / 'resumed').manifest()['winner'])

    assert tmc.chopper_freq_hz(run(), DRIVER) < 33000
    before = len(stub_printer.measured)
    assert tmc.chopper_freq_hz(run('--audible-khz', '33', '--skip-audible'), DRIVER) >= 33000
    # the combos the first run measured below the limit are not even re-measured
    assert all(tmc.chopper_freq_hz(combo, DRIVER) >= 33000
               for combo in stub_printer.measured[before:])
    # what a later ANALYZE or SAVE re-ranks with: the hearing the winner was picked with
    manifest = Dataset.open(tmp_path / 'resumed').manifest()
    assert tmc.Hearing.of(recorded=manifest) == tmc.Hearing(33000, tmc.AUDIBLE_WEIGHT, True)


def test_the_stock_reference_counts_when_the_hearing_skips_it(stub_printer):
    # stock 2/3/5/0 runs at 42.9 kHz: below 45 it is kept out of the ranking, yet the
    # run still says how much it beat Klipper's defaults by
    tune.run_tune(build_parser().parse_args(['tune', '--motor', 'a', '--speed', '58', '--no-raw',
                                             '--audible-khz', '45', '--skip-audible']))
    manifest = Dataset.open(stub_printer.roots[-1]).manifest()
    assert manifest['improvement'] > 1
    assert tmc.chopper_freq_hz(tmc.Chopper(**manifest['winner']), DRIVER) >= 45000


def test_the_landscape_marks_what_the_hearing_calls_audible():
    hearing = tmc.Hearing(limit_hz=33000)
    tbls, toffs, _, text = tbl_toff_matrix(rank(reported(), DRIVER, hearing), DRIVER, hearing)
    cell = {(tbl, toff): text[row][column] for row, tbl in enumerate(tbls)
            for column, toff in enumerate(toffs)}
    assert cell[(3, 7)].endswith('!') and cell[(3, 5)].endswith('!')
    assert not cell[(3, 4)].endswith('!')


def test_a_collect_with_an_unreachable_limit_moves_nothing(stub_printer, tmp_path):
    with pytest.raises(SystemExit, match='AUDIBLE_KHZ=90'):
        collect(stub_printer.kl(), build_parser().parse_args(
            ['collect', '--axis', 'x', '--speed', '58', '--search', 'descent', '--yes',
             '--no-raw', '--dataset', str(tmp_path / 'never'), '--audible-khz', '90',
             '--skip-audible']))
    assert 'MOVE' not in stub_printer.log


def reported_dataset(tmp_path) -> Dataset:
    """The #157 table as a run with AUDIBLE_KHZ=33 SKIP_AUDIBLE=1 that recorded no winner."""
    ds = Dataset.create(tmp_path / 'd', {'driver': '2209', 'stepper': 'stepper_x', 'trim': 0.1,
                                         **tmc.Hearing(33000, 0.25, True).manifest_fields()})
    for index, (fields, magnitude) in enumerate(REPORTED):
        ds.append({'id': str(index), 'kind': 'move', 'status': 'ok',
                   **tmc.Chopper(*fields).fields(), 'score': {'median_magnitude': magnitude}})
    return ds


def test_a_skipped_combo_scores_infinite_and_never_seeds_a_search(tmp_path):
    hearing = tmc.Hearing(33000, skip=True)
    assert penalized_score(tmc.Chopper(3, 7, 0, 0), [6614.7], DRIVER, hearing) == float('inf')
    ds = reported_dataset(tmp_path)
    assert seed_start(ds, DRIVER, tmc.Hearing()) == tmc.Chopper(3, 7, 0, 0)
    assert seed_start(ds, DRIVER, hearing) == tmc.Chopper(3, 4, 0, 0)


def test_save_re_ranks_a_dataset_as_its_run_heard(tmp_path):
    _, winner = tune.winner_of(str(reported_dataset(tmp_path).root))
    assert winner == tmc.Chopper(3, 4, 0, 0)


def test_analyze_hears_as_the_run_did_unless_told_otherwise(tmp_path, capsys):
    ds = reported_dataset(tmp_path)
    run_analyze(build_parser().parse_args(['analyze', str(ds.root), '--no-html']))
    out = capsys.readouterr().out
    assert '9 measured combos are left out: their chopper runs below 33 kHz' in out
    assert 'driver_TOFF: 4' in out
    run_analyze(build_parser().parse_args(['analyze', str(ds.root), '--no-html',
                                           '--audible-khz', '20']))
    assert 'driver_TOFF: 7' in capsys.readouterr().out
