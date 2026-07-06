import itertools

import pytest

from chopper_autotune import tmc
from chopper_autotune.analyze import hyst_matrix, rank, tbl_toff_matrix, write_report

DRIVER = tmc.DRIVERS['2209']


def make_ranked():
    aggregates = []
    for t, o, hs, he in itertools.product(range(0, 4), range(2, 9), range(0, 8, 2), range(0, 16, 4)):
        combo = tmc.Chopper(t, o, hs, he)
        if tmc.validate(combo) is not None:
            continue
        magnitude = 500 + 300 * abs(o - 6) + 50 * abs(hs - 4) + 20 * abs(he - 8)
        aggregates.append({'chopper': combo, 'magnitude': float(magnitude),
                           'spread': 30.0, 'n': 2})
    return rank(aggregates, DRIVER, audible_weight=0.25)


def test_rank_prefers_clean_over_slightly_quieter_clicky(tmp_path, capsys):
    from chopper_autotune.analyze import aggregate, print_table
    from chopper_autotune.dataset import Dataset
    ds = Dataset.create(tmp_path / 'ds', {})
    # measured case: the clicky config wins the median by ~4% but clicks every move
    for i, (combo, magnitude, clicks) in enumerate((
            (tmc.Chopper(2, 1, 7, 11), 1180.0, 5),
            (tmc.Chopper(2, 1, 7, 11), 1180.0, 6),
            (tmc.Chopper(2, 1, 5, 3), 1227.0, 0),
            (tmc.Chopper(2, 1, 5, 3), 1227.0, 0))):
        ds.append({'id': 'm%d' % i, 'kind': 'move', 'status': 'ok', **combo.fields(),
                   'score': {'median_magnitude': magnitude, 'clicks': clicks}})
    ranked = rank(aggregate(ds, False, 0.1), DRIVER, audible_weight=0.25)
    assert ranked[0]['chopper'] == tmc.Chopper(2, 1, 5, 3)          # clean wins
    assert ranked[1]['clicks'] == 11
    print_table(ranked, 5)
    out = capsys.readouterr().out
    assert 'clicks' in out


def test_tbl_toff_matrix_medians_and_audible_mark():
    ranked = make_ranked()
    tbls, toffs, z, text = tbl_toff_matrix(ranked, DRIVER)
    assert tbls == [0, 1, 2, 3]
    assert toffs == list(range(2, 9))
    # magnitude does not depend on tbl in the synthetic surface
    column = toffs.index(6)
    assert z[0][column] == z[3][column] == min(z[0])
    # tbl3/toff8 -> f_chop 14.9 kHz, must carry the audible mark
    assert text[3][toffs.index(8)].endswith('!')
    assert not text[0][toffs.index(2)].endswith('!')


def test_hyst_matrix_shape_and_values():
    ranked = make_ranked()
    hstrts, hends, z = hyst_matrix(ranked, tbl=0, toff=6)
    assert hstrts == [0, 2, 4, 6]
    assert hends == [0, 4, 8, 12]
    assert min(v for row in z for v in row if v is not None) == 500 + 0 + 0
    assert z[hstrts.index(4)][hends.index(8)] == 500.0


def test_write_report_produces_all_sections(tmp_path):
    pytest.importorskip('plotly')
    ranked = make_ranked()
    path = tmp_path / 'report.html'
    write_report(ranked, DRIVER, 'tmc2209 stepper_x', str(path))
    html = path.read_text()
    assert html.count('<div') >= 4
    assert 'chopper frequency landscape' in html
    assert 'hysteresis landscape' in html
    assert 'top 30 configurations' in html
    assert 'vibration vs chopper frequency' in html
    assert len(html) < 6_000_000
