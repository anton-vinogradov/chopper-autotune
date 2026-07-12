import pytest

from chopper_autotune.dataset import Dataset
from chopper_autotune.find_speed import (build_curve, cruise_for, find_peaks, recommend,
                                         scan_id, smooth)


def test_scan_sweeps_on_stock_registers_and_restores(tmp_path, monkeypatch):
    # a well-tuned config suppresses the very resonance peak the scan looks for,
    # so the sweep must run on Klipper defaults and restore the tuning afterwards
    import chopper_autotune.find_speed as fs
    from chopper_autotune import tmc
    from chopper_autotune.cli import build_parser
    from chopper_autotune.collect import Hardware

    class FakeKl:
        path = '<sock>'

        def __init__(self):
            self.scripts = []

        def gcode(self, script):
            self.scripts.append(script)

        def subscribe_accel(self, chip):
            pass

        def settings(self):
            return {}

        def is_printing(self):
            return False

    kl = FakeKl()
    baseline = {'tbl': 0, 'toff': 2, 'hstrt': 2, 'hend': 12}
    hw = Hardware(kl=kl, stepper='stepper_x', driver=tmc.DRIVERS['2209'],
                  accel_chip='adxl345', kinematics='corexy', axis_span=260,
                  center=(130, 130), max_accel=10000, baseline=baseline)
    monkeypatch.setattr(fs, 'detect_hardware', lambda kl_, axis: hw)
    monkeypatch.setattr(fs, 'measure_baseline', lambda hw_, ds, args, done: None)

    def fake_move(hw_, ds, args, record, speed, cruise, travel, direction, accel, before):
        record['status'] = 'ok'
        record['score'] = {'median_magnitude': 100.0}
        ds.append(record)
        return record

    monkeypatch.setattr(fs, 'measure_move', fake_move)
    args = build_parser().parse_args(
        ['find-speed', '--axis', 'x', '--yes', '--min-speed', '58', '--max-speed', '58',
         '--dataset', str(tmp_path / 'ds'), '--no-raw'])
    code, recommended = fs.scan(kl, args)
    assert code == 0 and recommended is None          # single flat point: no peaks

    sweep_set = next(s for s in kl.scripts if 'FIELD=hend' in s and 'VALUE=0' in s)
    restore_set = next(s for s in reversed(kl.scripts) if 'FIELD=hend' in s)
    assert 'VALUE=0' in sweep_set                     # Klipper default hend for the sweep
    assert 'VALUE=12' in restore_set                  # tuned baseline restored afterwards
    assert Dataset.open(tmp_path / 'ds').manifest()['scan_registers'] == \
        tmc.KLIPPER_DEFAULT.fields()


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


def test_rising_at_edge_flags_a_clipped_peak():
    from chopper_autotune.find_speed import rising_at_edge
    # the measured failure: monotonic rise into the range top = the peak is clipped
    rising = [(100, 1500), (110, 1800), (120, 2230)]
    assert rising_at_edge(rising, max_speed=120, step=2)
    # an interior maximum is not an edge case
    interior = [(40, 900), (58, 2676), (80, 1200), (120, 800)]
    assert not rising_at_edge(interior, max_speed=120, step=2)
    # too little data to judge
    assert not rising_at_edge([(20, 100), (22, 200)], max_speed=22, step=2)


def test_fit_max_speed_respects_the_travel_limit():
    from chopper_autotune.find_speed import MIN_CRUISE_SEC, cruise_for, fit_max_speed
    accel, limit, measure_time = 1000.0, 104.0, 1.0
    top = fit_max_speed(accel, limit, measure_time, step=2)
    assert cruise_for(top, accel, limit, measure_time) >= MIN_CRUISE_SEC
    assert cruise_for(top + 2, accel, limit, measure_time) < MIN_CRUISE_SEC
    assert top > 120                     # the old default was nowhere near the real ceiling


def test_cruise_for_zero_speed_is_zero_not_a_crash():
    assert cruise_for(0, 4000, 200, 2.0) == 0.0
