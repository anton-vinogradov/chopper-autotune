import argparse
import itertools

import pytest

from chopper_autotune import tmc
from chopper_autotune.analyze import run_compare, spearman
from chopper_autotune.collect import Range, build_plan
from chopper_autotune.dataset import Dataset

DRIVER = tmc.DRIVERS['2209']


def test_spearman():
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [40, 30, 20, 10]) == pytest.approx(-1.0)
    assert abs(spearman([1, 2, 3, 4, 5, 6], [2, 1, 4, 3, 6, 5])) < 1.0


def test_spearman_averages_tied_ranks():
    # ties must get averaged ranks: with ordinal ranks this pair returned 0.8
    assert spearman([1, 2, 2, 3], [1, 3, 2, 4]) == pytest.approx(0.9487, abs=1e-3)


def make_dataset(tmp_path, name, magnitude_of):
    ds = Dataset.create(tmp_path / name, {'driver': '2209', 'stepper': 'stepper_' + name})
    for toff, hend in itertools.product(range(2, 9), range(0, 8)):
        combo = tmc.Chopper(0, toff, 4, hend)
        ds.append({'id': '%s_v58_i0_fwd' % combo.label(), 'kind': 'move', 'status': 'ok',
                   'tbl': 0, 'toff': toff, 'hstrt': 4, 'hend': hend,
                   'score': {'median_magnitude': float(magnitude_of(combo))}})
    return ds


def test_compare_correlated_datasets(tmp_path, capsys):
    surface = lambda c: 500 + 200 * abs(c.toff - 6) + 30 * abs(c.hend - 5)
    ds_a = make_dataset(tmp_path, 'x', surface)
    ds_b = make_dataset(tmp_path, 'y', lambda c: 2 * surface(c))

    args = argparse.Namespace(dataset_a=str(ds_a.root), dataset_b=str(ds_b.root),
                              top=10, audible_weight=0.25)
    assert run_compare(args) == 0
    out = capsys.readouterr().out
    assert out.count('winner tbl0_toff6_hstrt4_hend5') == 2
    assert 'Common combos: 56' in out
    assert 'Spearman rank correlation: 1.000' in out
    assert 'Top-10 overlap: 10/10' in out
    assert 'Median magnitude scale B/A: 2.00' in out


def test_compare_anticorrelated(tmp_path, capsys):
    ds_a = make_dataset(tmp_path, 'x', lambda c: 100 + 10 * c.toff + c.hend)
    ds_b = make_dataset(tmp_path, 'y', lambda c: 1000 - (10 * c.toff + c.hend))

    args = argparse.Namespace(dataset_a=str(ds_a.root), dataset_b=str(ds_b.root),
                              top=5, audible_weight=0.25)
    run_compare(args)
    out = capsys.readouterr().out
    rho = float(out.split('rank correlation: ')[1].split()[0])
    assert rho < -0.9
    assert 'Top-5 overlap: 0/5' in out


def test_build_plan_skip_audible():
    ranges = (Range(0, 3), Range(1, 8), Range(4, 4), Range(4, 4))
    full = build_plan(DRIVER, *ranges, None, [58])
    quiet = build_plan(DRIVER, *ranges, None, [58], skip_audible=True)
    assert len(quiet) < len(full)
    assert all(not tmc.is_audible(combo, DRIVER) for combo, _ in quiet)
    # tbl2/toff8 (19.7 kHz) is in the full plan but not the quiet one
    assert any(c == tmc.Chopper(2, 8, 4, 4) for c, _ in full)
    assert all(c != tmc.Chopper(2, 8, 4, 4) for c, _ in quiet)
