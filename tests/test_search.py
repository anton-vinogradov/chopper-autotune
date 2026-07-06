import argparse
import itertools

import pytest

from chopper_autotune import tmc
from chopper_autotune.collect import Range
from chopper_autotune.dataset import Dataset
from chopper_autotune.search import (_spanning_starts, coordinate_descent, dataset_history,
                                     descent_budget, multi_start_descent, penalized_score,
                                     run_simulate, seed_start)

DRIVER = tmc.DRIVERS['2209']
RANGES = {'tbl': Range(0, 3), 'toff': Range(1, 8), 'hstrt': Range(0, 7), 'hend': Range(0, 15)}


def surface(combo):
    """Separable bowl with the optimum at tbl1 toff6 hstrt4 hend7."""
    return (300 * abs(combo.toff - 6) + 100 * abs(combo.tbl - 1)
            + 50 * abs(combo.hstrt - 4) + 20 * abs(combo.hend - 7) + 500)


def descend(evaluate, tpfd=None, driver=DRIVER, start=tmc.Chopper(2, 3, 5, 0)):
    cache = {}
    def cached(combo):
        if combo not in cache:
            cache[combo] = evaluate(combo)
        return cache[combo]
    best = coordinate_descent(driver, RANGES['tbl'], RANGES['toff'], RANGES['hstrt'],
                              RANGES['hend'], tpfd, start, cached)
    return best, cache


def test_descent_finds_separable_optimum():
    best, cache = descend(surface)
    assert best == tmc.Chopper(1, 6, 4, 7)
    grid_size = sum(1 for t, o, hs, he in itertools.product(
        RANGES['tbl'].values(), RANGES['toff'].values(),
        RANGES['hstrt'].values(), RANGES['hend'].values())
        if tmc.validate(tmc.Chopper(t, o, hs, he)) is None)
    assert len(cache) <= descent_budget(DRIVER, *RANGES.values(), None)
    assert len(cache) < grid_size / 20


def test_descent_never_evaluates_invalid():
    def checked(combo):
        assert tmc.validate(combo) is None
        return surface(combo)
    descend(checked)


def test_descent_respects_audible_penalty():
    # magnitude improves mildly with toff, but toff >= 9 pushes f_chop below 20 kHz;
    # the doubled audible score must outweigh the small vibration gain
    def audible_trap(combo):
        magnitude = 3000.0 - 100 * combo.toff
        return penalized_score(combo, [magnitude], DRIVER, audible_weight=1.0)

    ranges = dict(RANGES, toff=Range(1, 15))
    best = coordinate_descent(DRIVER, ranges['tbl'], ranges['toff'], ranges['hstrt'],
                              ranges['hend'], None, tmc.Chopper(2, 3, 5, 0), audible_trap)
    assert not tmc.is_audible(best, DRIVER)


def test_descent_stops_when_stable():
    calls = []
    def counting(combo):
        calls.append(combo)
        return surface(combo)
    descend(counting)
    assert len(calls) == len(set(calls))  # cache wrapper: every combo evaluated once


def make_grid_dataset(tmp_path):
    ds = Dataset.create(tmp_path / 'grid', {
        'driver': '2209', 'stepper': 'stepper_x',
        'baseline_registers': {'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 0},
    })
    for t, o, hs, he in itertools.product(range(0, 4), range(1, 9), range(0, 8), range(0, 16)):
        combo = tmc.Chopper(t, o, hs, he)
        if tmc.validate(combo) is not None:
            continue
        for direction in ('fwd', 'rev'):
            ds.append({'id': '%s_v58_i0_%s' % (combo.label(), direction), 'kind': 'move',
                       'status': 'ok', 'tbl': t, 'toff': o, 'hstrt': hs, 'hend': he,
                       'score': {'median_magnitude': surface(combo)}})
    return ds


def test_simulate_on_synthetic_grid(tmp_path, capsys):
    ds = make_grid_dataset(tmp_path)
    args = argparse.Namespace(dataset=str(ds.root), audible_weight=0.25)
    assert run_simulate(args) == 0
    out = capsys.readouterr().out
    assert 'Descent best: tbl1_toff6_hstrt4_hend7' in out
    assert 'Gap to global optimum: 0.0%' in out


def test_seed_start_picks_penalized_best_and_adapts_tpfd(tmp_path):
    ds = Dataset.create(tmp_path / 'seed', {})
    quiet = {'tbl': 0, 'toff': 8, 'hstrt': 7, 'hend': 5, 'tpfd': 4}     # 21.1 kHz
    whiny = {'tbl': 3, 'toff': 8, 'hstrt': 7, 'hend': 5, 'tpfd': 4}     # 18.6 kHz, audible
    for name, fields, magnitude in (('a', quiet, 1000.0), ('b', whiny, 900.0)):
        ds.append({'id': name, 'kind': 'move', 'status': 'ok', **fields,
                   'score': {'median_magnitude': magnitude}})

    # audible 900 * 1.5 loses to quiet 1000; tpfd stripped for a driver without it
    best = seed_start(ds, DRIVER, audible_weight=0.5)
    assert best == tmc.Chopper(0, 8, 7, 5)
    # for a TPFD-capable driver the seed keeps tpfd, and a small weight flips the winner
    best5160 = seed_start(ds, tmc.DRIVERS['5160'], audible_weight=0.05)
    assert best5160 == tmc.Chopper(3, 8, 7, 5, tpfd=4)


def test_seed_start_empty_dataset(tmp_path):
    ds = Dataset.create(tmp_path / 'empty', {})
    with pytest.raises(SystemExit):
        seed_start(ds, DRIVER, 0.25)


def nonseparable(combo):
    """Two valleys interacting on (toff, hend): a shallow one at toff8/low-hend that a
    low-hend start descends into, and a deeper global one at toff2/high-hend that is
    invisible until hend is high — the exact blind spot the reference grid exposed."""
    base = 10 * abs(combo.hstrt - 4) + 20 * combo.tbl
    at_low = 100 * abs(combo.toff - 8) + 8 * combo.hend
    at_high = 100 * abs(combo.toff - 2) + 8 * (15 - combo.hend) - 40
    return 1000 + base + min(at_low, at_high)


def test_multi_start_escapes_the_toff_hend_blind_spot():
    ranges = {'tbl': Range(0, 0), 'toff': Range(1, 8), 'hstrt': Range(0, 7), 'hend': Range(0, 15)}
    baseline = tmc.Chopper(0, 3, 4, 0)
    cache = {}
    def cached(combo):
        if combo not in cache:
            cache[combo] = nonseparable(combo)
        return cache[combo]

    single = coordinate_descent(DRIVER, ranges['tbl'], ranges['toff'], ranges['hstrt'],
                                ranges['hend'], None, baseline, cached)
    multi = multi_start_descent(DRIVER, ranges['tbl'], ranges['toff'], ranges['hstrt'],
                                ranges['hend'], None, baseline, cached)

    assert single.toff == 8 and single.hend == 0        # stuck in the shallow valley
    assert multi == tmc.Chopper(0, 2, 4, 14)            # deep global one (hend 15 breaks 18-limit)
    assert nonseparable(multi) < nonseparable(single)


def test_spanning_starts_valid_and_spread():
    # tbl 2:3 keeps toff=1 valid so all three toff levels survive
    starts = _spanning_starts(Range(2, 3), Range(1, 8), Range(0, 7), Range(0, 15))
    assert all(tmc.validate(c) is None for c in starts)
    assert {c.toff for c in starts} == {1, 4, 8}
    assert {c.hend for c in starts} == {0, 7, 15}


def test_dataset_history_groups_directions(tmp_path):
    ds = Dataset.create(tmp_path / 'ds', {})
    for direction, magnitude in (('fwd', 100.0), ('rev', 200.0)):
        ds.append({'id': 'x_%s' % direction, 'kind': 'move', 'status': 'ok',
                   'tbl': 1, 'toff': 5, 'hstrt': 4, 'hend': 4,
                   'score': {'median_magnitude': magnitude}})
    history = dataset_history(ds)
    assert history[tmc.Chopper(1, 5, 4, 4)] == [100.0, 200.0]


def test_dataset_transients_sums_clicks(tmp_path):
    from chopper_autotune.search import dataset_transients
    ds = Dataset.create(tmp_path / 'ds', {})
    ds.append({'id': 'a_fwd', 'kind': 'move', 'status': 'ok',
               'tbl': 1, 'toff': 5, 'hstrt': 4, 'hend': 4,
               'score': {'median_magnitude': 100.0, 'clicks': 2}})
    ds.append({'id': 'a_rev', 'kind': 'move', 'status': 'ok',
               'tbl': 1, 'toff': 5, 'hstrt': 4, 'hend': 4,
               'score': {'median_magnitude': 100.0}})          # pre-clicks record
    assert dataset_transients(ds)[tmc.Chopper(1, 5, 4, 4)] == 2


def test_click_penalty_beats_a_small_median_win():
    # measured case: h16 wins the median by ~4% but clicks ~5x per move;
    # the penalty must hand the win to the clean config
    clean = penalized_score(tmc.Chopper(2, 1, 5, 3), [1227.0], DRIVER, 0.25)
    clicky = penalized_score(tmc.Chopper(2, 1, 7, 11), [1180.0], DRIVER, 0.25,
                             clicks_per_move=5.5)
    assert clean < clicky
    # and a click-free score is unchanged by the new argument
    assert clean == penalized_score(tmc.Chopper(2, 1, 5, 3), [1227.0], DRIVER, 0.25,
                                    clicks_per_move=0.0)
