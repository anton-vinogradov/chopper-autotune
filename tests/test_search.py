import argparse
import itertools

import pytest

from chopper_autotune import tmc
from chopper_autotune.collect import Range
from chopper_autotune.dataset import Dataset
from chopper_autotune.search import (coordinate_descent, dataset_history, descent_budget,
                                     penalized_score, run_simulate)

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


def test_dataset_history_groups_directions(tmp_path):
    ds = Dataset.create(tmp_path / 'ds', {})
    for direction, magnitude in (('fwd', 100.0), ('rev', 200.0)):
        ds.append({'id': 'x_%s' % direction, 'kind': 'move', 'status': 'ok',
                   'tbl': 1, 'toff': 5, 'hstrt': 4, 'hend': 4,
                   'score': {'median_magnitude': magnitude}})
    history = dataset_history(ds)
    assert history[tmc.Chopper(1, 5, 4, 4)] == [100.0, 200.0]
