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


def test_agreeing_tolerance_covers_pump_line_scatter():
    from chopper_autotune.belts import agreeing
    # field: front-span pump tries landed 308.3 and 301.6 (2.2% apart) and were
    # rejected at 2% — the pump lines scatter wider than the fundamentals
    assert agreeing([308.3], 301.6) is not None
    assert agreeing([308.3], 295.0) is None          # 4.4%: still a real disagreement


def test_state_default_paths_split_pluck_from_sweep():
    # belts.json is what the panel renders as TENSION — the sweep's structural
    # frequencies must live in their own file (the falsified reading, measured)
    assert belts_mod.SWEEP_STATE != belts_mod.STATE


def test_polar_class_names_the_clean_axis():
    from chopper_autotune.belts import polar_class
    assert polar_class((285.0, 150.0, 0.9, 0.1)) == 'X'
    assert polar_class((285.0, 150.0, 0.2, 0.8)) == 'Y'
    assert polar_class((285.0, 150.0, 0.5, 0.5)) == '?'   # mixed = structural mode
    assert polar_class((285.0, 150.0)) == '?'             # no polarization data


def test_window_tones_polarization_follows_the_motion_axis():
    import numpy as np

    from chopper_autotune.belts import _window_tones
    fs, seconds = 3200.0, 1.2
    t = np.arange(int(fs * seconds)) / fs
    tone = np.sin(2 * np.pi * 285.0 * t)
    # motion purely along chip axis 0; machine X mapped to that axis
    acc = np.stack([tone * 4000, np.random.default_rng(1).normal(0, 3, t.size),
                    np.random.default_rng(2).normal(0, 3, t.size)], axis=1)
    rot = (np.array([1.0, 0.0, 0.0]), np.array([0.0, 1.0, 0.0]))
    tones = _window_tones(acc, fs, (40.0, 1000.0), 3, rot=rot)
    assert tones and abs(tones[0][0] - 285.0) < 2.0
    assert tones[0][2] > 0.9                              # X share dominates


def test_axis_direction_pca_separates_a_45_degree_mount():
    # per-axis RMS reads both jogs of a 45deg-mounted chip as the same direction;
    # the principal component keeps the sign and tells them apart
    import numpy as np
    fs = 3200.0
    t = np.arange(int(fs * 1.0)) / fs
    burst = np.sin(2 * np.pi * 30 * t) * 2000
    x_jog = np.stack([burst * 0.707, burst * 0.707, np.zeros_like(burst)], axis=1)
    y_jog = np.stack([burst * 0.707, -burst * 0.707, np.zeros_like(burst)], axis=1)

    def pca(acc):
        _, vecs = np.linalg.eigh(np.cov(acc.T))
        return vecs[:, -1]

    ex, ey = pca(x_jog), pca(y_jog)
    assert abs(float(np.dot(ex, ey))) < 0.1          # orthogonal, as they truly are
    rms = lambda a: np.sqrt((a ** 2).mean(axis=0)) / np.linalg.norm(np.sqrt((a ** 2).mean(axis=0)))
    assert abs(float(np.dot(rms(x_jog), rms(y_jog)))) > 0.9   # the bug PCA fixes


def test_exclude_ambient_handles_polarized_tuples():
    from chopper_autotune.belts import exclude_ambient
    tones = [(285.0, 150.0, 0.9, 0.1), (144.0, 90.0, 0.2, 0.8)]
    ambient = [(285.5, 40.0, 0.5, 0.5)]                 # 4-tuples on both sides
    left = exclude_ambient(tones, ambient)
    assert [t[0] for t in left] == [144.0]


def test_families_and_resolver_reproduce_the_field_verdict():
    """The exact failing field run: belt A's true front-span pump (293, pol=X) sat one
    SNR notch below a side-span line (88, pol=Y) and lost a greedy accept; the family
    resolver must pick the X pair shared with belt B -> matched fundamentals."""
    from chopper_autotune.belts import resolve_pair, tone_families
    a_tries = [
        [(331.1, 39.0, 0.8, 0.2)],
        [(367.2, 88.0, 0.5, 0.5)],
        [(133.5, 98.0, 0.8, 0.2), (293.0, 42.0, 0.8, 0.2)],
        [(87.7, 333.0, 0.2, 0.8), (294.0, 316.0, 0.8, 0.2), (134.0, 87.0, 0.7, 0.3)],
        [(86.5, 341.0, 0.2, 0.8), (274.0, 212.0, 0.5, 0.5)],
    ]
    b_tries = [
        [(293.6, 302.0, 0.8, 0.2), (276.0, 212.0, 0.5, 0.5), (91.0, 171.0, 0.3, 0.7)],
        [(292.8, 43.0, 0.8, 0.2)],
    ]
    fams_a, fams_b = tone_families(a_tries), tone_families(b_tries)
    assert any(f['cls'] == 'Y' for f in fams_a)      # the side-span decoy IS there
    pair = resolve_pair(fams_a, fams_b)
    assert pair is not None
    fam_a, fam_b = pair
    assert fam_a['cls'] == fam_b['cls'] == 'X'       # the shared front-span family wins
    assert abs(fam_a['freq'] - 293.5) < 2 and abs(fam_b['freq'] - 293.2) < 2
    # halved: ~146.8 vs ~146.6 -> matched, exactly what the phone said


def test_resolver_refuses_when_no_shared_family():
    from chopper_autotune.belts import resolve_pair
    assert resolve_pair([{'freq': 88, 'cls': 'Y', 'tries': 2, 'snr': 300}],
                        [{'freq': 293, 'cls': 'X', 'tries': 2, 'snr': 300}]) is None
