"""CoreXY belt-diagonal response comparison. On a symmetric CoreXY, matched belts give
matched responses, so a persistent gap between the diagonals is worth investigating —
but honestly: a gap can be tension mismatch OR structural asymmetry, and on the
reference rig the dominant response did NOT track tension at all (a heavy overtension
of belt B moved its 131 Hz response by nothing — the mode is structural there). So the
tool reports the two responses and their run-to-run deltas, and it watches for that
exact failure: if a belt was tensioned between runs and its response did not move, it
says so instead of asking for more turns. For absolute tension use the pluck test (the
transverse string mode, f = sqrt(T/mu)/2L — a different mode from what an axial sweep
through the motor excites).

Each diagonal is excited alone by Klipper's swept-sine TEST_RESONANCES (A = head 1,1
drives motor A only; B = head 1,-1 drives motor B only — the same single-motor split as
the chopper stress test). We take Klipper's *raw* capture and compute the response
spectrum here (numpy in our own venv), so nothing needs numpy inside klippy-env; only a
configured [resonance_tester] (as for input-shaper calibration).

The default flow is the tension measurement proper: you pluck each belt's span like a
guitar string on the display's cue and the toolhead accelerometer listens. The pluck excites
the transverse string mode — the anchor shakes the head laterally at f and, because the
string's tension pulses twice per cycle, axially at 2f; the tool identifies the (f, 2f)
pair and reports the fundamental (a lone unpaired line is suspect — a weak pluck often
shows only a harmonic, measured: a 4f line masquerading at 400 Hz). Equal spans ->
matched fundamentals = matched tension; with SPAN= (cm) it also reports absolute
newtons, T = mu * (2 * L * f)^2.
"""
from __future__ import annotations

import glob
import os

import numpy as np

from .collect import (Screen, await_flushed, capture_span, coupled_xy, detect_hardware,
                      motor_label, refuse_if_printing, run_restore)
from .current import stress_vector
from .dataset import load_json, save_json
from .klippy import Klippy, find_socket

SEGMENT = 1024              # Welch window; ~0.3 s at the ADXL's ~3.2 kHz -> ~3 Hz resolution
MATCH_TOLERANCE = 5.0       # percent apart below which the belts count as matched
MAX_HZ_PER_SEC = 2.0        # Klipper caps the sweep rate here
STATE = os.path.expanduser('~/printer_data/config/chopper-autotune/belts.json')
# the sweep's run-to-run deltas live apart: its structural response frequencies must
# never land in belts.json, which the panel renders as TENSION (pluck fundamentals)
SWEEP_STATE = os.path.expanduser('~/printer_data/config/chopper-autotune/belts_sweep.json')

PLUCK_BAND = (40.0, 1000.0)  # near-head spans ring ~200-450 Hz, their 2f up to ~900;
                             # ambient lines (e.g. a persistent ~600 Hz) are excluded
                             # via the quiet reference instead of capping the band
PLUCK_SNR = 30.0            # a pluck tone must beat the window's median spectrum by this
PAIR_TOLERANCE = 0.04       # |f2 - 2*f1| / f2 for an (f, 2f) match
GT2_MU = 7.7                # g/m, GT2 6 mm with a fiberglass core


def gap_pct(freq_a: float, freq_b: float) -> float:
    return abs(freq_a - freq_b) / ((freq_a + freq_b) / 2) * 100


def load_state(path: 'str | None' = None) -> 'dict | None':
    return load_json(path or STATE) or None


def save_state(freq_a: float, freq_b: float, path: 'str | None' = None):
    save_json(path or STATE, {'A': freq_a, 'B': freq_b})


def progress_message(freq_a: float, freq_b: float, prev: 'dict | None',
                     tolerance: float = MATCH_TOLERANCE) -> str:
    """A one-line status for the display: the gap now, how it changed since the previous run
    (per belt), and which diagonal responds lower — deliberately NOT a "tighten X" order:
    a gap can be structure, and the per-belt deltas are what tell you if a turn did anything."""
    gap = gap_pct(freq_a, freq_b)
    now = '%.0f/%.0fHz' % (freq_a, freq_b)
    if prev and 'A' in prev and 'B' in prev:
        change = 'gap %.1f->%.1f%% (A%+.0f B%+.0f)' % (
            gap_pct(prev['A'], prev['B']), gap, freq_a - prev['A'], freq_b - prev['B'])
    else:
        change = 'gap %.1f%%' % gap
    if gap < tolerance:
        return 'Diag matched %s %s' % (now, change)
    lower = 'A' if freq_a < freq_b else 'B'
    return '%s lower %s %s' % (lower, now, change)


def insensitive(freqs: 'dict[str, float]', prev: 'dict | None', threshold_hz: float = 3.0) -> bool:
    """True when nothing moved since the previous run — the signal that if a belt WAS
    tensioned in between, the response does not track tension on this machine (measured
    on the reference rig: a heavy overtension moved the response by 0 Hz)."""
    if not prev or 'A' not in prev or 'B' not in prev:
        return False
    return (abs(freqs['A'] - prev['A']) < threshold_hz
            and abs(freqs['B'] - prev['B']) < threshold_hz)


def wait_for_capture(pattern: str, min_span_sec: float = 0.0, timeout: float = 30.0) -> str:
    """collect.await_flushed with a sweep-shaped error: TEST_RESONANCES returns before
    the background writer has flushed the raw CSV, and a truncated sweep reads as a
    phantom peak at whatever frequency the cut landed on (measured: '156 Hz')."""
    try:
        return await_flushed(pattern, min_span_sec, timeout, poll=0.3)
    except TimeoutError:
        files = glob.glob(pattern)
        newest = max(files, key=os.path.getmtime) if files else None
        raise SystemExit('capture incomplete for %s: %.0fs of the %.0fs sweep flushed — '
                         'check the [resonance_tester] output path'
                         % (pattern, capture_span(newest) if newest else 0.0, min_span_sec))


def welch_psd(path: str) -> 'tuple[np.ndarray, np.ndarray]':
    """Welch PSD (segmented, Hann-windowed, summed over axes) of a Klipper raw-accel CSV."""
    data = np.loadtxt(path, delimiter=',', comments='#')
    times, acc = data[:, 0], data[:, 1:4]
    fs = 1.0 / np.median(np.diff(times))
    acc = acc - acc.mean(axis=0)
    window = np.hanning(SEGMENT)
    psd = None
    segments = 0
    for start in range(0, len(acc) - SEGMENT, SEGMENT // 2):
        block = acc[start:start + SEGMENT] * window[:, None]
        power = (np.abs(np.fft.rfft(block, axis=0)) ** 2).sum(axis=1)
        psd = power if psd is None else psd + power
        segments += 1
    if segments == 0:
        raise SystemExit('belt capture too short to analyze (%s)' % os.path.basename(path))
    return np.fft.rfftfreq(SEGMENT, 1 / fs), psd / segments


def dominant(freqs: np.ndarray, psd: np.ndarray, band: 'tuple[float, float]') -> 'tuple[float, float]':
    """Dominant response frequency within `band` as the energy centroid of the strongest
    region. A real belt answers with a comb of nearby span modes at comparable energies
    (measured: 138/153/162 Hz within 8 %), so a bare argmax jitters between the teeth by
    several bins run-to-run; the centroid of the contiguous >=50 %-of-max region is stable."""
    smooth = np.convolve(psd, np.ones(5) / 5, mode='same')
    mask = (freqs >= band[0]) & (freqs <= band[1])
    f, p = freqs[mask], smooth[mask]
    if len(p) == 0 or not np.isfinite(p).any():
        raise SystemExit('no spectrum in %.0f-%.0f Hz — capture too short or sample rate too low'
                         % band)
    top = int(np.argmax(p))
    lo, hi = top, top
    while lo > 0 and p[lo - 1] >= 0.5 * p[top]:
        lo -= 1
    while hi < len(p) - 1 and p[hi + 1] >= 0.5 * p[top]:
        hi += 1
    region = slice(lo, hi + 1)
    centroid = float((f[region] * p[region]).sum() / p[region].sum())
    return centroid, float(freqs[1] - freqs[0])


def top_peaks(freqs: np.ndarray, psd: np.ndarray, band: 'tuple[float, float]',
              count: int = 3) -> 'list[float]':
    """The strongest local maxima within `band`, for the console — so a multi-peak comb
    (several belt spans) is visible instead of hiding behind the single number."""
    mask = (freqs >= band[0]) & (freqs <= band[1])
    f, p = freqs[mask], psd[mask]
    peaks = [(p[i], f[i]) for i in range(1, len(p) - 1) if p[i - 1] < p[i] > p[i + 1]]
    return [freq for _, freq in sorted(peaks, reverse=True)[:count]]


def welch_peak(path: str, band: 'tuple[float, float]') -> 'tuple[float, float]':
    freqs, psd = welch_psd(path)
    return dominant(freqs, psd, band)


def verdict(freq_a: float, freq_b: float, tolerance: float = MATCH_TOLERANCE) -> str:
    apart = gap_pct(freq_a, freq_b)
    if apart < tolerance:
        return ('matched: %.1f%% apart (< %.0f%%) — the two diagonals respond alike.'
                % (apart, tolerance))
    lower = 'A' if freq_a < freq_b else 'B'
    return ('GAP: %.1f%% — diagonal %s responds lower. That can be a looser belt %s OR a '
            'structural asymmetry: verify with the pluck test before turning anything, and '
            'if you do adjust, make a SMALL change and watch the per-belt delta on the next '
            'run — if the number does not move, stop: the response does not track tension '
            'on this machine.' % (apart, lower, lower))


def identify_belt(kl: Klippy, hw, motor: str, screen: Screen, cycles: int = 4):
    """Jog the head along this belt's diagonal so ONLY its loop moves — on CoreXY a 1,-1
    move is pure motor B, leaving belt A still — so the user can see which belt to adjust —
    then release the gantry motors so the belt is easy to reach and tension."""
    cx, cy = hw.center
    vec = stress_vector(hw.kinematics, motor)
    span = min(40.0, hw.axis_span / 6)
    label = motor_label(motor)
    print('Jogging belt %s so you can see which one it is, then releasing the motors...' % label)
    screen.update('Belt %s — the moving one' % label, force=True)
    kl.gcode('G90\nG1 X%.1f Y%.1f F6000\nM400' % (cx, cy))
    moves = []
    for _ in range(cycles):
        moves += ['G1 X%.1f Y%.1f F4800' % (cx + span * vec[0], cy + span * vec[1]),
                  'G1 X%.1f Y%.1f F4800' % (cx - span * vec[0], cy - span * vec[1])]
    moves.append('G1 X%.1f Y%.1f F6000' % (cx, cy))
    # release the gantry motors (not Z) so the loosened belt is easy to reach and tension
    kl.gcode('\n'.join(moves) + '\nM400\n'
             'SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=0\n'
             'SET_STEPPER_ENABLE STEPPER=stepper_y ENABLE=0')


def run_belts(args) -> int:
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        return belts(kl, args)
    finally:
        kl.close()


def belts(kl: Klippy, args) -> int:
    hw = detect_hardware(kl, 'x')
    settings = kl.settings()
    if not coupled_xy(hw.kinematics):
        raise SystemExit('belt-tension match is a CoreXY/H-bot check (two belts drive one '
                         'motion); kinematics here is %s — nothing to match' % hw.kinematics)
    if getattr(args, 'pluck', False) and args.sweep:
        raise SystemExit('PLUCK=1 and SWEEP=1 contradict — pick one (pluck is the default)')

    if args.show:                                   # just point at a belt, no measurement
        motor = 'x' if args.show == 'a' else 'y'
        if args.dry_run:
            print('DRY_RUN: would home X/Y, jog motor %s and switch motors off'
                  % motor_label(motor))
            return 0
        screen = Screen(kl, hw.display)
        refuse_if_printing(kl)
        kl.gcode('G28 X Y\nM400')
        identify_belt(kl, hw, motor, screen)
        screen.final('Motors off — belt %s is the one that moved' % motor_label(motor))
        return 0                                     # leaves the motors off on purpose

    if not args.sweep:                              # the pluck tension test is the default
        return pluck_mode(kl, hw, args)

    tester = settings.get('resonance_tester') or {}
    if not tester.get('probe_points'):
        raise SystemExit('needs a [resonance_tester] with probe_points (same as Klipper '
                         'input-shaper calibration) — TEST_RESONANCES drives the excitation')

    band = (float(args.min_freq), float(args.max_freq))
    hz_per_sec = min(args.hz_per_sec, MAX_HZ_PER_SEC)   # Klipper rejects a faster sweep
    if hz_per_sec < args.hz_per_sec:
        print('Capping sweep rate to %g Hz/s (Klipper maximum)' % MAX_HZ_PER_SEC)
    print('Belt-diagonal response comparison on %s: swept-sine %g-%g Hz per diagonal '
          '(motor A = head 1,1, motor B = head 1,-1), response spectrum computed here.'
          % (hw.kinematics, band[0], band[1]))
    if args.dry_run:
        return 0
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1

    refuse_if_printing(kl)
    screen = Screen(kl, hw.display)
    peaks = {}
    try:
        print('Homing all axes (TEST_RESONANCES moves to the probe point)')
        kl.gcode('G28\nM400')
        for motor in ('x', 'y'):
            label = motor_label(motor)
            vec = stress_vector(hw.kinematics, motor)
            axis = '%g,%g' % vec
            for stale in glob.glob('/tmp/raw_data_*belt%s*.csv' % label):
                os.remove(stale)
            screen.update('Chopper belts: exciting %s' % label, force=True)
            print(' exciting belt %s (head diagonal %s)...' % (label, axis))
            kl.gcode('TEST_RESONANCES AXIS=%s OUTPUT=raw_data NAME=belt%s '
                     'FREQ_START=%g FREQ_END=%g HZ_PER_SEC=%g\nM400'
                     % (axis, label, band[0], band[1], hz_per_sec))
            path = wait_for_capture('/tmp/raw_data_*belt%s*.csv' % label,
                                    min_span_sec=(band[1] - band[0]) / hz_per_sec)
            freqs, psd = welch_psd(path)
            peak, binwidth = dominant(freqs, psd, band)
            peaks[label] = peak
            edge = '  (near the sweep edge — raise MAX_FREQ)' if peak >= band[1] - 2 * binwidth else ''
            print('   belt %s: resonance %.1f Hz (peaks: %s)%s'
                  % (label, peak, ', '.join('%.0f' % f for f in top_peaks(freqs, psd, band)), edge))
    finally:
        run_restore(lambda: kl.gcode('G28 X Y'))

    prev = load_state(SWEEP_STATE)                  # the previous run, to show what changed
    save_state(peaks['A'], peaks['B'], SWEEP_STATE)
    print('\n=== Belt-diagonal response ===')
    print('Diagonal A %.1f Hz  |  Diagonal B %.1f Hz' % (peaks['A'], peaks['B']))
    print(verdict(peaks['A'], peaks['B'], args.tolerance))
    if prev and 'A' in prev and 'B' in prev:
        print('Since last run: gap %.1f%% -> %.1f%%  (A %+.1f, B %+.1f Hz)'
              % (gap_pct(prev['A'], prev['B']), gap_pct(peaks['A'], peaks['B']),
                 peaks['A'] - prev['A'], peaks['B'] - prev['B']))
        if insensitive(peaks, prev):
            print('Nothing moved since the last run. If you changed a belt tension in '
                  'between, the response does NOT track tension on this machine — do not '
                  'keep tightening; check absolute tension with the pluck test instead.')

    message = progress_message(peaks['A'], peaks['B'], prev, args.tolerance)
    if gap_pct(peaks['A'], peaks['B']) >= args.tolerance:
        # release the gantry so the belts are easy to reach; the Motor A/B buttons
        # (SHOW=) point at a motor when needed
        kl.gcode('SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=0\n'
                 'SET_STEPPER_ENABLE STEPPER=stepper_y ENABLE=0')
        message += ' · motors off'
    screen.final(message)
    print('\nIf you adjust a belt, change it a LITTLE and re-run: the per-belt delta tells '
          'you whether the response follows the tension at all. After any mechanical change, '
          're-run CHOPPER_TUNE — the chopper optimum is measured against the mechanics you '
          'leave in place.')
    return 0


def _window_tones(acc: np.ndarray, fs: float, band: 'tuple[float, float]',
                  count: int, rot: 'tuple | None' = None) -> 'list[tuple]':
    acc = acc - acc.mean(axis=0)
    n = acc.shape[0]
    window = np.hanning(n)[:, None]
    nfft = 1 << int(np.ceil(np.log2(4 * n)))
    ffts = np.fft.rfft(acc * window, n=nfft, axis=0)
    spec = (np.abs(ffts) ** 2).sum(axis=1)
    if rot is not None:
        # power of each line along the MACHINE axes: the axial tension pump shakes the
        # frame along the belt, the transverse wave across it — polarization tells the
        # harmonic's nature where a phone's microphone would just hear the fundamental
        ex, ey = rot
        px = np.abs(ffts @ ex) ** 2
        py = np.abs(ffts @ ey) ** 2
    freqs = np.fft.rfftfreq(nfft, 1 / fs)
    mask = (freqs >= band[0]) & (freqs <= band[1])
    f, p = freqs[mask], spec[mask]
    median = np.median(p)
    peaks = [(p[i], f[i], i) for i in range(2, len(p) - 2)
             if p[i - 1] < p[i] > p[i + 1] and p[i] > PLUCK_SNR * median]
    peaks.sort(reverse=True)
    if rot is not None:
        fx, fy = px[mask], py[mask]
    out, taken = [], []
    for power, freq, i in peaks:
        if all(abs(freq - t) > 5.0 for t in taken):
            if rot is not None:
                total = fx[i] + fy[i]
                shares = (float(fx[i] / total), float(fy[i] / total)) if total > 0 else (0.5, 0.5)
                out.append((float(freq), float(power / median)) + shares)
            else:
                out.append((float(freq), float(power / median)))
            taken.append(freq)
        if len(out) >= count:
            break
    return out


def exclude_ambient(tones: 'list[tuple[float, float]]',
                    ambient: 'list[tuple[float, float]] | None') -> 'list[tuple[float, float]]':
    """Drop tones that were already ringing in the quiet reference (fans, electronics —
    measured: a persistent ~600 Hz line on the reference rig)."""
    if not ambient:
        return tones
    return [t for t in tones
            if all(abs(t[0] - a) > max(6.0, 0.02 * a) for a, _ in ambient)]


def pluck_tones(samples: np.ndarray, band: 'tuple[float, float]' = PLUCK_BAND,
                ambient: 'list[tuple[float, float]] | None' = None,
                count: int = 6, rot: 'tuple | None' = None) -> 'list[tuple]':
    """Tonal peaks of a pluck capture as (freq, snr-over-median), strongest first.
    The pluck ring lives in a fraction of the capture and decays fast, so a whole-window
    FFT dilutes it below the noise (measured: near-head plucks read "nothing heard");
    scan overlapping ~1.2 s sub-windows and take the loudest one instead."""
    fs = 1.0 / np.median(np.diff(samples[:, 0]))
    acc = samples[:, 1:4]
    step = max(1, int(1.2 * fs))
    best, best_snr = [], 0.0
    for start in range(0, max(1, acc.shape[0] - step + 1), step // 2):
        tones = exclude_ambient(_window_tones(acc[start:start + step], fs, band, count,
                                              rot=rot), ambient)
        if tones and tones[0][1] > best_snr:
            best, best_snr = tones, tones[0][1]
    return best


def fundamental(tones: 'list[tuple[float, float]]') -> 'tuple[float | None, bool]':
    """The string fundamental from a pluck's tone list. A pluck shakes the anchor
    laterally at f and axially at 2f (the tension pulses twice per cycle), so an
    (f, 2f) pair identifies the fundamental unambiguously; a lone line is returned
    unpaired=False — it may be a harmonic of a weak pluck (measured: 4f at 400 Hz)."""
    best = None
    for freq, snr, *_ in tones:
        for freq2, snr2, *_ in tones:
            if freq2 > freq and abs(freq2 - 2 * freq) <= PAIR_TOLERANCE * freq2:
                combined = snr + snr2
                if best is None or combined > best[1]:
                    best = (freq, combined)
    if best is not None:
        return best[0], True
    return (tones[0][0], False) if tones else (None, False)


def polar_class(tone: tuple) -> str:
    """'X'/'Y' when a line's energy is clearly along one machine axis, '?' otherwise
    (mixed = a structural mode, not a clean belt line)."""
    if len(tone) < 4:
        return '?'
    _, _, share_x, share_y = tone[:4]
    if share_x >= 0.65:
        return 'X'
    if share_y >= 0.65:
        return 'Y'
    return '?'


def machine_axes(hw, kl) -> 'tuple | None':
    """Map the chip's axes to the MACHINE's by feeling two gentle jogs: the phone app
    hears the fundamental because its microphone lives in the air; our chip lives on
    the structure and hears the tension pump at 2f along the belt — telling along from
    across is what lets us halve the right lines. None when the jogs read ambiguous."""
    from .collect import capture_stream

    def direction(letter):
        _, s = capture_stream(hw, 'G91\nG1 %s-5 F1800\nG1 %s5 F1800\nG90\nM400'
                              % (letter, letter), 1.2)
        acc = s[:, 1:4] - s[:, 1:4].mean(axis=0)
        # the first principal component, not per-axis RMS: RMS drops the sign, and a
        # chip mounted at 45 deg to the machine then reads X and Y jogs as the SAME
        # direction (field: 'Axis calibration: ambiguous' on an honestly mounted V0)
        _, vecs = np.linalg.eigh(np.cov(acc.T))
        return vecs[:, -1]

    ex, ey = direction('X'), direction('Y')
    if abs(float(np.dot(ex, ey))) > 0.5:            # jogs read alike: mounting is odd,
        return None                                 # fall back to unpolarized analysis
    return ex, ey


def tension_newtons(freq: float, span_cm: float, mu_g_per_m: float = GT2_MU) -> float:
    """T = mu * (2 * L * f)^2 — the transverse string mode, the one that IS tension."""
    wave_speed = 2 * (span_cm / 100.0) * freq
    return (mu_g_per_m / 1000.0) * wave_speed * wave_speed


def pluck_mode(kl: Klippy, hw, args) -> int:
    """Guided pluck on each belt's LONGEST free span (across the front on most CoreXY —
    field-tested: near-head spans are too short and stiff to ring usefully). A belt is
    accepted once two plucks agree on the paired fundamental within 2% — repeatability is
    the control. Motors stay enabled DURING the measurement (the pulley is held, so the
    span has defined ends) and are released at the end: the user's next move is a
    tensioner screw, and holding motors would fight it."""
    from .collect import capture_stream
    print('Pluck test: the head parks at the REAR so the side spans are at their longest. '
          'On the display cue, pluck the LONGEST free span of each belt (a side span on '
          'small printers, the front span on big ones) mid-span, like a guitar string — '
          'pull ~5 mm sideways, release sharply. Pluck HARD; equal spans for both belts. '
          'Each belt needs two agreeing plucks.')
    if args.dry_run:
        return 0
    refuse_if_printing(kl)
    screen = Screen(kl, hw.display)
    kl.subscribe_accel(hw.accel_chip)
    # park at the REAR, X centered (user idea, field-born on a 120 mm V0): the side
    # spans between the front idlers and the gantry are then at their longest — twice
    # the center-parked length. f ~ 1/L, but the win is amplitude and ring time: a
    # longer span is softer at mid-point and decays slower, which is exactly what a
    # short stiff belt lacked. Left/right side spans stay equal (X centered), so the
    # A-vs-B comparison stays honest; the front span is unchanged for big machines.
    cx, _ = hw.center
    rear_y = float(kl.settings()['stepper_y']['position_max']) - 3.0
    kl.gcode('G28 X Y\nG90\nG1 X%.1f Y%.1f F6000\nM400' % (cx, rear_y))

    rot = machine_axes(hw, kl)
    print('Axis calibration: %s' % ('ok — lines will carry polarization (along-belt = '
                                    'tension pump at 2f)' if rot else
                                    'ambiguous — falling back to unpolarized analysis'))

    def cue(text):
        screen.update(text, force=True)
        print('>> %s' % text, flush=True)

    print('Capturing the quiet reference (do not touch)...')
    _, quiet = capture_stream(hw, 'G4 P3000', 2.8)
    ambient = pluck_tones(quiet, rot=rot)
    if ambient:
        print('   ambient lines excluded: %s' % ', '.join('%.0f Hz' % f for f, _ in ambient))

    def agreeing(seen: 'list[float]', new: float, tol: float = 0.02) -> 'float | None':
        """The match for `new` among earlier tries, within tol — repeatability is the
        control, and it need not be consecutive (mixed excitations between plucks)."""
        for old in seen:
            if abs(new - old) <= tol * new:
                return old
        return None

    def measure_belt(label):
        paired_seen, lone_seen = [], []
        for attempt in range(1, args.plucks + 1):
            cue('Ready: belt %s in 3s' % label)
            kl.gcode('G4 P3000')
            cue('PLUCK belt %s now! (listening 5s...)' % label)
            _, samples = capture_stream(hw, 'G4 P5000', 4.8)
            tones = pluck_tones(samples, ambient=ambient, rot=rot)
            freq, paired = fundamental(tones)
            if freq is None:
                cue('Belt %s: nothing heard — again' % label)
                print('   belt %s try %d: nothing heard' % (label, attempt))
                continue
            cls = next((polar_class(t) for t in tones if abs(t[0] - freq) < 1.0), '?')
            note = 'f=%.1f Hz %s%s  [%s]' % (
                freq, 'paired f+2f' if paired else 'unpaired',
                ' pol=%s' % cls if cls != '?' else '',
                ', '.join('%.0f(x%.0f)' % t[:2] for t in tones[:3]))
            print('   belt %s try %d: %s' % (label, attempt, note))
            if paired:
                match = agreeing(paired_seen, freq)
                if match is not None:
                    print('   belt %s: %.1f / %.1f Hz agree — accepted' % (label, match, freq))
                    return {'fund': (match + freq) / 2, 'line': (match + freq) / 2,
                            'via': 'paired', 'cls': cls}
                paired_seen.append(freq)
                cue('Belt %s heard %.0f Hz — once more' % (label, freq))
                continue
            # no (f, 2f) pair: through the STRUCTURE the tension pump (2f, along the
            # belt) often arrives alone — the transverse f dies in the stiff frame
            # (measured on a 120 mm V0; a phone's microphone hears f through the AIR,
            # which is why it needs no such reasoning). A repeated lone line polarized
            # along one machine axis IS that pump: halve it for the fundamental.
            match = agreeing(lone_seen, freq)
            if match is not None:
                line = (match + freq) / 2
                if cls in ('X', 'Y'):
                    print('   belt %s: %.1f / %.1f Hz agree, polarized %s — the axial '
                          'pump; fundamental = %.1f Hz' % (label, match, freq, cls, line / 2))
                    return {'fund': line / 2, 'line': line, 'via': 'pump', 'cls': cls}
                print('   belt %s: %.1f / %.1f Hz agree (unpaired, mixed polarization) — '
                      'accepted by repeatability, harmonic order UNKNOWN'
                      % (label, match, freq))
                return {'fund': line, 'line': line, 'via': 'lone', 'cls': cls}
            lone_seen.append(freq)
            cue('Belt %s heard %.0f Hz (unpaired) — once more' % (label, freq))
        cue('FAILED: belt %s gave no stable tone' % label)         # the display must say why
        raise SystemExit('belt %s: no two agreeing plucks in %d tries — pluck harder, '
                         'mid-span, and re-run' % (label, args.plucks))

    fundamentals = {}
    try:
        for label in ('A', 'B'):
            fundamentals[label] = measure_belt(label)
    finally:
        # hand the gantry over on every exit — verdict, failed plucks or Stop — the
        # user's next move is a tensioner screw; no parting G28: releasing forgets the
        # position anyway and every next job homes first
        run_restore(lambda: kl.gcode('SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=0\n'
                                     'SET_STEPPER_ENABLE STEPPER=stepper_y ENABLE=0'))

    ra, rb = fundamentals['A'], fundamentals['B']
    # comparing different span families (an X-polarized front line vs a Y-polarized
    # side line, or unknown harmonic orders) produced flip-flopping verdicts in the
    # field — refuse instead of comparing apples to echoes of oranges
    if 'lone' in (ra['via'], rb['via']):
        raise SystemExit('harmonic order unknown for belt %s — pluck the SAME span on '
                         'both belts and re-run (the lines had mixed polarization)'
                         % ('A' if ra['via'] == 'lone' else 'B'))
    if ra['via'] == rb['via'] == 'pump' and '?' not in (ra['cls'], rb['cls']) \
            and ra['cls'] != rb['cls']:
        raise SystemExit('belts A and B answered from DIFFERENT span families '
                         '(A along %s, B along %s) — pluck the SAME span on both '
                         'belts and re-run' % (ra['cls'], rb['cls']))
    fa, fb = ra['fund'], rb['fund']
    for label, r in (('A', ra), ('B', rb)):
        if r['via'] == 'pump':
            print('belt %s: measured %.1f Hz along %s = the tension pump (2f) -> '
                  'fundamental %.1f Hz' % (label, r['line'], r['cls'], r['fund']))
    # tension goes as the SQUARE of frequency, so a 3% frequency gap is a ~6% tension
    # gap — say it in percent with the f^2 hint, or the number reads as a contradiction
    tension_gap = ((fa / fb) ** 2 - 1) * 100
    print('\n=== Belt tension (pluck) ===')
    print('Belt A fundamental %.1f Hz  |  Belt B %.1f Hz' % (fa, fb))
    print('Tension A vs B: %+.0f%%  (T ~ f^2: (%.1f/%.1f)^2 = %.2f)'
          % (tension_gap, fa, fb, (fa / fb) ** 2))
    if args.span:
        ta, tb = tension_newtons(fa, args.span, args.mu), tension_newtons(fb, args.span, args.mu)
        print('At SPAN=%.0f cm: T_A ~ %.0f N, T_B ~ %.0f N' % (args.span, ta, tb))
    if abs(tension_gap) < args.tolerance * 2:        # tolerance is in freq %; tension ~ 2x
        message = 'Belts matched: A %.0f / B %.0f Hz (tension %+.0f%%)' % (fa, fb, tension_gap)
    else:
        looser = 'A' if fa < fb else 'B'
        message = 'Belt %s looser: A %.0f / B %.0f Hz (tension %+.0f%%)' % (looser, fa, fb,
                                                                            tension_gap)
    print(message)
    save_state(fa, fb)
    screen.final(message + ' \u00b7 motors off')
    print('\nThis is the transverse string mode — the one that IS tension (f ~ sqrt(T)); '
          'equal spans compare directly. After adjusting, re-run to confirm the move.')
    return 0
