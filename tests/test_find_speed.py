import pytest

from chopper_autotune.dataset import Dataset
from chopper_autotune.find_speed import (build_curve, cruise_for, find_peaks, recommend,
                                         scan_id, smooth)


def test_cruise_for_respects_travel_limit():
    assert cruise_for(50, 1000, 104, 1.0) == 1.0
    # 120 mm/s: accel path 14.4mm leaves 89.6mm -> 0.747s of cruise
    assert cruise_for(120, 1000, 104, 1.0) == pytest.approx((104 - 14.4) / 120)


def test_find_peaks_two_humps():
    curve = [100, 120, 500, 900, 520, 140, 130, 300, 1400, 2100, 1300, 400, 200]
    peaks = find_peaks(curve)
    assert [curve[i] for i in peaks] == [900, 2100]


def test_find_peaks_ignores_noise_and_flat():
    assert find_peaks([100.0] * 10) == []
    noisy = [100, 104, 99, 103, 101, 98, 102, 100]
    assert find_peaks(noisy, prominence_ratio=1.0) == []


def test_smooth_keeps_length_and_ends():
    values = [1.0, 10.0, 1.0, 10.0, 1.0]
    smoothed = smooth(values)
    assert len(smoothed) == 5
    assert smoothed[0] == 1.0 and smoothed[-1] == 1.0
    assert smoothed[1] == pytest.approx(4.0)


def test_recommend_skips_weak_low_peak():
    # real case from the first hardware sweep: 34 mm/s hump is 2.4x weaker than 58 mm/s
    curve = [(32, 1100), (34, 1120), (36, 1000), (56, 2000), (58, 2676), (60, 2100)]
    assert recommend(curve, [1, 4]) == 58
    # comparable peaks: prefer the lowest speed
    assert recommend([(50, 2000), (100, 2100)], [0, 1]) == 50
    assert recommend([(50, 2000)], []) is None


def test_scan_id():
    assert scan_id(55, 0, 1) == 'v055_i0_fwd'
    assert scan_id(120, 2, -1) == 'v120_i2_rev'


def test_build_curve_medians(tmp_path):
    ds = Dataset.create(tmp_path / 'ds', {'mode': 'find-speed'})
    for speed, magnitudes in ((40, (100, 140)), (42, (900, 1100))):
        for i, magnitude in enumerate(magnitudes):
            ds.append({'id': 'v%03d_%d' % (speed, i), 'kind': 'speed', 'status': 'ok',
                       'speed': speed, 'score': {'median_magnitude': magnitude}})
    ds.append({'id': 'baseline', 'kind': 'baseline', 'status': 'ok',
               'score': {'median_magnitude': 50}})
    ds.append({'id': 'v040_bad', 'kind': 'speed', 'status': 'failed', 'speed': 40})

    assert build_curve(ds) == [(40, 120), (42, 1000)]
