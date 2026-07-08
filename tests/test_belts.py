import pytest
import numpy as np

from chopper_autotune import belts as belts_mod
from chopper_autotune.belts import (gap_pct, load_state, progress_message, save_state, verdict,
                                    wait_for_capture, welch_peak)


def _raw_csv(path, freq, fs=3200.0, seconds=1.5):
    """A Klipper-style raw-accel CSV with a single sine tone on one axis."""
    t = np.arange(0, seconds, 1 / fs)
    ax = np.sin(2 * np.pi * freq * t)
    rows = np.column_stack([t, ax, np.zeros_like(t), np.zeros_like(t)])
    with open(path, 'w') as fh:
        fh.write('#time,accel_x,accel_y,accel_z\n')
        for r in rows:
            fh.write('%.6f,%.4f,%.4f,%.4f\n' % tuple(r))


def test_welch_peak_finds_the_tone(tmp_path):
    csv = tmp_path / 'raw.csv'
    _raw_csv(str(csv), freq=130.0)
    peak, binwidth = welch_peak(str(csv), band=(20.0, 200.0))
    assert abs(peak - 130.0) <= binwidth        # within one FFT bin


def test_welch_peak_respects_the_band(tmp_path):
    # a strong tone outside the band must not be picked
    csv = tmp_path / 'raw.csv'
    _raw_csv(str(csv), freq=250.0)
    peak, _ = welch_peak(str(csv), band=(20.0, 200.0))
    assert peak <= 200.0


def test_verdict_balanced_and_mismatch():
    assert 'balanced' in verdict(155.0, 153.0)                 # ~1.3% apart
    m = verdict(155.0, 133.0)                                  # ~15% apart, B lower
    assert 'MISMATCH' in m and 'belt B' in m and 'Tighten belt B' in m
    a = verdict(133.0, 155.0)                                  # A lower now
    assert 'belt A' in a and 'Tighten belt A' in a


def test_verdict_tolerance_is_configurable():
    # 8% apart: matched under a 10% tolerance, a mismatch under the 5% default
    assert 'balanced' in verdict(104.0, 96.0, tolerance=10.0)
    assert 'MISMATCH' in verdict(104.0, 96.0, tolerance=5.0)


def test_wait_for_capture_returns_a_settled_file(tmp_path):
    csv = tmp_path / 'raw_data_beltA.csv'
    csv.write_text('#time,accel_x,accel_y,accel_z\n0.0,1,0,0\n')
    assert wait_for_capture(str(tmp_path / 'raw_data_*beltA*.csv'), timeout=5.0) == str(csv)


def test_wait_for_capture_times_out_on_nothing(tmp_path):
    with pytest.raises(SystemExit, match='no usable capture'):
        wait_for_capture(str(tmp_path / 'raw_data_*.csv'), timeout=0.5)


def test_belts_macro_args_translate():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    args = parser.parse_args(_gcode_args(
        ['belts', 'MIN_FREQ=40', 'MAX_FREQ=180', 'TOLERANCE=8', 'DRY_RUN=1'],
        boolean_flags(parser)))
    assert (args.min_freq == 40 and args.max_freq == 180 and args.tolerance == 8
            and args.dry_run)


def test_progress_message_first_run_and_delta():
    # first run: no previous state -> gap and which to tighten, no delta
    first = progress_message(156.0, 132.0, prev=None)
    assert 'Tighten B' in first and 'gap 16.7%' in first and 'A 156 / B 132 Hz' in first
    assert '->' not in first
    # second run: B came up 9 Hz -> show the change per belt and the closing gap
    second = progress_message(156.0, 141.0, prev={'A': 156.0, 'B': 132.0})
    assert 'B+9' in second and 'A+0' in second
    assert '16.7->' in second and 'Tighten B' in second


def test_progress_message_matched():
    msg = progress_message(156.0, 154.0, prev={'A': 156.0, 'B': 141.0})
    assert msg.startswith('Belts matched') and 'B+13' in msg


def test_gap_pct():
    assert gap_pct(156.0, 132.0) == pytest.approx(16.67, abs=0.1)
    assert gap_pct(150.0, 150.0) == 0.0


def test_state_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(belts_mod, 'STATE', str(tmp_path / 'belts.json'))
    assert load_state() is None                      # nothing yet
    save_state(156.0, 132.0)
    assert load_state() == {'A': 156.0, 'B': 132.0}


def test_belts_show_and_identify_args():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    # SHOW=B -> just jog belt B; case-insensitive
    args = parser.parse_args(_gcode_args(['belts', 'SHOW=B'], boolean_flags(parser)))
    assert args.show == 'b' and not args.no_identify
    # NO_IDENTIFY=1 -> skip the post-measurement jog
    args = parser.parse_args(_gcode_args(['belts', 'NO_IDENTIFY=1'], boolean_flags(parser)))
    assert args.no_identify and args.show is None
