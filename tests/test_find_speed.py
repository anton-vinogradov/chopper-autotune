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

        def settings(self):
            return {}

        def stepper_states(self):
            return {'stepper_x': True, 'stepper_y': True, 'stepper_z': True}

        def gcode(self, script):
            self.scripts.append(script)

        def gcode_output(self, script):
            return []

        def subscribe_accel(self, chip):
            pass

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


def scan_failing(stub_printer, monkeypatch, failing_every: int, rising: bool = False,
                 command: str = 'find-speed', fails_above: int = 10 ** 6):
    """The real scan on the stub printer; every `failing_every`-th move fails, and every
    move above `fails_above` mm/s, the first failure with the driver error. `rising`: a
    curve still rising at the range edge."""
    import chopper_autotune.find_speed as find_speed
    from chopper_autotune import resonance_map
    from chopper_autotune.cli import build_parser
    moves = stub_printer.speeds = []

    def move(hw, ds, args, record, speed, cruise, travel, direction, accel, before_move):
        moves.append(speed)
        if len(moves) % failing_every == 0 or speed > fails_above:
            first = not any(r.get('status') == 'failed' for r in ds.records())
            record.update(status='failed', error='drv_err=1' if first else 'TimeoutError')
        else:
            record.update(status='ok', score={'median_magnitude': float(speed) if rising
                                              else 2000.0 - 30 * abs(speed - 60)})
        ds.append(record)
        return record
    monkeypatch.setattr(find_speed, 'measure_move', move)
    args = build_parser().parse_args([command, '--axis', 'x', '--yes', '--no-raw'])
    if command == 'map':
        return resonance_map.resonance_map(stub_printer.kl(), args)
    return find_speed.scan(stub_printer.kl(), args)


def test_a_scan_that_mostly_failed_names_its_first_error(stub_printer, monkeypatch):
    # #133: 95 of 102 moves failed on a driver error, and the user read 'no clear peaks'
    with pytest.raises(SystemExit, match=r'^motor A: the speed scan failed on \d+ of \d+ moves; '
                                         r'the first error: drv_err=1$'):
        scan_failing(stub_printer, monkeypatch, failing_every=2)


def test_a_few_failed_scan_moves_still_give_a_verdict(stub_printer, monkeypatch, capsys):
    code, recommended = scan_failing(stub_printer, monkeypatch, failing_every=10)
    assert code == 2 and recommended == 60
    assert 'scan moves failed; the first error: drv_err=1' in capsys.readouterr().out


def test_a_failing_scan_is_not_extended_to_faster_moves(stub_printer, monkeypatch):
    # a rising curve asks for faster moves: not on a setup that fails half of them
    with pytest.raises(SystemExit, match='motor A: the speed scan failed'):
        scan_failing(stub_printer, monkeypatch, failing_every=2, rising=True)
    assert max(stub_printer.speeds) <= 120


def test_a_map_of_a_mostly_failed_scan_is_no_map(stub_printer, monkeypatch):
    # CHOPPER_MAP draws its peaks and dips from the same sweep: its holes read as dips
    with pytest.raises(SystemExit, match='motor A: the speed scan failed'):
        scan_failing(stub_printer, monkeypatch, failing_every=2, command='map')


def test_failures_of_a_range_this_run_does_not_measure_do_not_count(tmp_path):
    # a resumed dataset may hold the failed moves of an earlier, wider scan
    from chopper_autotune.find_speed import planned_ids, refuse_a_failed_scan
    ds = Dataset.create(tmp_path / 'resumed', {})
    plan = [(speed, 1.0) for speed in (20, 22)]
    for speed, status in ((20, 'ok'), (22, 'ok'), (160, 'failed'), (180, 'failed'), (200, 'failed')):
        for direction in (1, -1):
            ds.append({'id': scan_id(speed, 0, direction), 'kind': 'speed', 'status': status,
                       'error': 'drv_err=1'})
    refuse_a_failed_scan(ds, 'A', planned_ids(plan, 1))
    with pytest.raises(SystemExit, match='failed on 6 of 10 moves'):
        refuse_a_failed_scan(ds, 'A', planned_ids(plan + [(160, 1.0), (180, 1.0), (200, 1.0)], 1))


def test_failures_of_the_extended_range_count_too(stub_printer, monkeypatch):
    # the curve rises past 120, and every faster move fails
    with pytest.raises(SystemExit, match='motor A: the speed scan failed on 40 of 142 moves'):
        scan_failing(stub_printer, monkeypatch, failing_every=10 ** 6, rising=True, fails_above=120)


def test_a_map_with_many_peaks_names_on_the_display_those_that_fit(stub_printer, monkeypatch):
    # the conftest guard holds every M117 to a 16-character LCD row
    import chopper_autotune.find_speed as find_speed
    from chopper_autotune import resonance_map
    from chopper_autotune.cli import build_parser

    def move(hw, ds, args, record, speed, cruise, travel, direction, accel, before_move):
        record.update(status='ok', score={'median_magnitude': 1000.0 + 600 * (speed // 10 % 2)})
        ds.append(record)
        return record
    monkeypatch.setattr(find_speed, 'measure_move', move)
    monkeypatch.setattr(resonance_map, 'save_state', lambda *args: None)
    assert resonance_map.resonance_map(stub_printer.kl(), build_parser().parse_args(
        ['map', '--axis', 'x', '--yes', '--no-raw'])) == 0
    shown = [line[len('M117 '):] for line in stub_printer.log if line.startswith('M117 A pk')]
    assert shown and shown[-1].count(',') >= 2


def test_a_map_asked_about_a_print_speed_answers_it_on_the_display(stub_printer, monkeypatch):
    # PRINT_SPEED asks whether that speed rings: the answer is what the display keeps
    import re

    import chopper_autotune.find_speed as find_speed
    from chopper_autotune import resonance_map
    from chopper_autotune.cli import build_parser

    def move(hw, ds, args, record, speed, cruise, travel, direction, accel, before_move):
        record.update(status='ok', score={'median_magnitude': 1000.0 + 600 * (speed // 10 % 2)})
        ds.append(record)
        return record
    monkeypatch.setattr(find_speed, 'measure_move', move)
    monkeypatch.setattr(resonance_map, 'save_state', lambda *args: None)
    resonance_map.resonance_map(stub_printer.kl(), build_parser().parse_args(
        ['map', '--axis', 'x', '--yes', '--no-raw', '--print-speed', '50']))
    shown = [line[len('M117 '):] for line in stub_printer.log if line.startswith('M117 ')]
    assert re.match(r'^A 50( ok|>\d)', shown[-1]), shown[-1]
