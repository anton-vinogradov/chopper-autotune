import pytest
import numpy as np

from chopper_autotune import belts as belts_mod
from chopper_autotune.belts import (capture_span, fundamental, gap_pct, insensitive,
                                    load_state, progress_message, save_state,
                                    tension_newtons, verdict, wait_for_capture, welch_peak)


def _raw_csv(path, freq, fs=3200.0, seconds=1.5, tones=None):
    """A Klipper-style raw-accel CSV with sine tone(s) on one axis."""
    t = np.arange(0, seconds, 1 / fs)
    ax = np.zeros_like(t)
    for f, amp in (tones or [(freq, 1.0)]):
        ax += amp * np.sin(2 * np.pi * f * t)
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


def test_welch_peak_stable_on_a_comb(tmp_path):
    """A real belt answers with a comb of near-equal span modes (measured 138/153/162 within
    8%); a bare argmax jitters between the teeth as their heights breathe run-to-run. The
    centroid must land mid-cluster and move only slightly when the tallest tooth flips."""
    a = tmp_path / 'a.csv'
    b = tmp_path / 'b.csv'
    _raw_csv(str(a), None, seconds=3.0, tones=[(138, 0.77), (153, 0.96), (162, 1.0)])
    _raw_csv(str(b), None, seconds=3.0, tones=[(138, 0.77), (153, 1.0), (162, 0.96)])
    peak_a, _ = welch_peak(str(a), band=(20.0, 200.0))
    peak_b, _ = welch_peak(str(b), band=(20.0, 200.0))
    assert 145.0 <= peak_a <= 165.0
    assert abs(peak_a - peak_b) < 5.0       # argmax would jump 162 -> 153 (9 Hz)


def test_capture_span_reads_the_edges(tmp_path):
    csv = tmp_path / 'raw.csv'
    _raw_csv(str(csv), freq=100.0, seconds=2.0)
    assert capture_span(str(csv)) == pytest.approx(2.0, abs=0.01)


def test_wait_for_capture_rejects_a_truncated_sweep(tmp_path):
    # size is stable but the capture covers only 2 s of an expected 60 s sweep
    csv = tmp_path / 'raw_data_beltA.csv'
    _raw_csv(str(csv), freq=100.0, seconds=2.0)
    with pytest.raises(SystemExit, match='incomplete'):
        wait_for_capture(str(tmp_path / 'raw_data_*beltA*.csv'), min_span_sec=60.0, timeout=2.5)


def test_verdict_matched_and_gap():
    assert 'matched' in verdict(155.0, 153.0)                  # ~1.3% apart
    m = verdict(155.0, 133.0)                                  # ~15% apart, B lower
    assert 'GAP' in m and 'diagonal B responds lower' in m
    # honesty: no tighten order, and the structural caveat is spelled out
    assert 'Tighten belt' not in m and 'structural' in m and 'pluck test' in m
    assert 'diagonal A responds lower' in verdict(133.0, 155.0)


def test_verdict_tolerance_is_configurable():
    # 8% apart: matched under a 10% tolerance, a gap under the 5% default
    assert 'matched' in verdict(104.0, 96.0, tolerance=10.0)
    assert 'GAP' in verdict(104.0, 96.0, tolerance=5.0)


def test_insensitive_flags_a_frozen_response():
    # nothing moved since the last run -> the tension-does-not-track warning fires
    assert insensitive({'A': 153.5, 'B': 131.0}, {'A': 153.7, 'B': 131.4})
    # a belt actually moved -> no warning
    assert not insensitive({'A': 153.7, 'B': 142.0}, {'A': 153.7, 'B': 131.4})
    # no previous run -> nothing to compare
    assert not insensitive({'A': 153.7, 'B': 131.4}, None)


def test_wait_for_capture_returns_a_settled_file(tmp_path):
    csv = tmp_path / 'raw_data_beltA.csv'
    _raw_csv(str(csv), freq=100.0, seconds=2.0)
    assert wait_for_capture(str(tmp_path / 'raw_data_*beltA*.csv'),
                            min_span_sec=1.5, timeout=5.0) == str(csv)


def test_wait_for_capture_times_out_on_nothing(tmp_path):
    with pytest.raises(SystemExit, match='incomplete'):
        wait_for_capture(str(tmp_path / 'raw_data_*.csv'), timeout=0.8)


def test_belts_macro_args_translate():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    args = parser.parse_args(_gcode_args(
        ['belts', 'MIN_FREQ=40', 'MAX_FREQ=180', 'TOLERANCE=8', 'DRY_RUN=1'],
        boolean_flags(parser)))
    assert (args.min_freq == 40 and args.max_freq == 180 and args.tolerance == 8
            and args.dry_run)


def test_progress_message_first_run_and_delta():
    # first run: no previous state -> gap and which diagonal is lower, no delta, no order
    first = progress_message(156.0, 132.0, prev=None)
    assert 'B lower' in first and 'gap 16.7%' in first and '156/132Hz' in first
    assert '->' not in first and 'Tighten' not in first
    # second run: B came up 9 Hz -> show the change per belt and the closing gap
    second = progress_message(156.0, 141.0, prev={'A': 156.0, 'B': 132.0})
    assert 'B+9' in second and 'A+0' in second
    assert '16.7->' in second and 'B lower' in second


def test_progress_message_matched():
    msg = progress_message(156.0, 154.0, prev={'A': 156.0, 'B': 141.0})
    assert msg.startswith('Diag matched') and 'B+13' in msg


def test_gap_pct():
    assert gap_pct(156.0, 132.0) == pytest.approx(16.67, abs=0.1)
    assert gap_pct(150.0, 150.0) == 0.0


def test_state_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(belts_mod, 'STATE', str(tmp_path / 'belts.json'))
    assert load_state() is None                      # nothing yet
    save_state(156.0, 132.0)
    assert load_state() == {'A': 156.0, 'B': 132.0}


def test_fundamental_prefers_the_f_2f_pair():
    # the measured belt-A pluck: f=101.6 (x385) + 2f=203.3 (x1162) + stray lines;
    # the fundamental is the PAIRED lower line even though 2f is the strongest
    tones = [(203.3, 1162.0), (101.6, 385.0), (107.5, 76.0), (113.4, 62.0), (209.1, 42.0)]
    freq, paired = fundamental(tones)
    assert paired and freq == 101.6


def test_fundamental_flags_a_lone_harmonic():
    # the measured weak-pluck trap: only a 400 Hz line (4f) visible -> unpaired
    freq, paired = fundamental([(400.6, 59.0)])
    assert freq == 400.6 and not paired
    assert fundamental([]) == (None, False)


def test_tension_newtons():
    # T = mu * (2 L f)^2: GT2 6mm, 35 cm span, 104.6 Hz -> ~41 N
    t = tension_newtons(104.6, span_cm=35.0, mu_g_per_m=7.7)
    assert t == pytest.approx(41.3, abs=1.0)
    # tension goes as f^2: +5% frequency -> ~+10% tension
    assert tension_newtons(109.8, 35.0, 7.7) / t == pytest.approx(1.10, abs=0.01)


def _pluck_samples(freq, fs=3200.0, total=4.8, burst_at=2.0, burst_len=0.6, amp=1.0,
                   ambient_freq=None):
    """A pluck capture: a decaying tone burst somewhere inside a long window, plus an
    optional always-on ambient line (a fan)."""
    t = np.arange(0, total, 1 / fs)
    ax = 0.02 * np.random.default_rng(7).standard_normal(t.size)
    burst = (t >= burst_at) & (t < burst_at + burst_len)
    ax[burst] += amp * np.sin(2 * np.pi * freq * t[burst]) * np.exp(-(t[burst] - burst_at) / 0.2)
    if ambient_freq:
        ax += 0.15 * np.sin(2 * np.pi * ambient_freq * t)
    return np.column_stack([t, ax, np.zeros_like(t), np.zeros_like(t)])


def test_pluck_tones_finds_a_short_burst_in_a_long_window():
    # a weak 0.6 s ring inside 4.8 s: a whole-window FFT dilutes it (the measured
    # "nothing heard" failure); the sub-window scan must catch it
    samples = _pluck_samples(246.0, amp=0.4)
    tones = belts_mod.pluck_tones(samples)
    assert tones and abs(tones[0][0] - 246.0) < 3.0


def test_pluck_tones_excludes_the_ambient_line():
    # a persistent ~600 Hz line (measured on the rig) must not be reported as a pluck
    samples = _pluck_samples(246.0, amp=0.4, ambient_freq=600.0)
    quiet = _pluck_samples(0.0, amp=0.0, ambient_freq=600.0)
    ambient = belts_mod.pluck_tones(quiet)
    assert any(abs(f - 600.0) < 6.0 for f, _ in ambient)
    tones = belts_mod.pluck_tones(samples, ambient=ambient)
    assert tones and abs(tones[0][0] - 246.0) < 3.0
    assert all(abs(f - 600.0) > 6.0 for f, _ in tones)


def test_pluck_macro_args_translate():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    # pluck is the default; PLUCK=1 stays accepted for compatibility
    args = parser.parse_args(_gcode_args(
        ['belts', 'PLUCK=1', 'SPAN=35', 'PLUCKS=6'], boolean_flags(parser)))
    assert not args.sweep and args.span == 35 and args.plucks == 6 and args.mu == 7.7
    # the sweep diagnostic is now the opt-in
    args = parser.parse_args(_gcode_args(['belts', 'SWEEP=1'], boolean_flags(parser)))
    assert args.sweep
    assert not parser.parse_args(_gcode_args(['belts'], boolean_flags(parser))).sweep


def test_belts_show_args():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    # SHOW=B -> just jog belt B; case-insensitive
    args = parser.parse_args(_gcode_args(['belts', 'SHOW=B'], boolean_flags(parser)))
    assert args.show == 'b'


def test_state_default_paths_split_pluck_from_sweep():
    # belts.json is what the panel renders as TENSION — the sweep's structural
    # frequencies must live in their own file (the falsified reading, measured)
    assert belts_mod.SWEEP_STATE != belts_mod.STATE
