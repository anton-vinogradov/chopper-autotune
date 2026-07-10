"""CoreXY belt-tension match. The two belts should resonate at the same frequency —
belt resonance goes as sqrt(tension), so a lower peak means a looser belt.

Each belt is excited alone by Klipper's swept-sine TEST_RESONANCES on that motor's
diagonal (A = head 1,1 drives motor A only; B = head 1,-1 drives motor B only — the
same single-motor split as the chopper stress test). We take Klipper's *raw* capture
and compute the response spectrum here (numpy in our own venv), so nothing needs numpy
inside klippy-env; only a configured [resonance_tester] (as for input-shaper calibration).
"""
from __future__ import annotations

import glob
import json
import os
import time

import numpy as np

from .collect import Screen, coupled_xy, detect_hardware, motor_label, refuse_if_printing, run_restore
from .current import stress_vector
from .klippy import Klippy, find_socket

SEGMENT = 1024              # Welch window; ~0.3 s at the ADXL's ~3.2 kHz -> ~3 Hz resolution
MATCH_TOLERANCE = 5.0       # percent apart below which the belts count as matched
MAX_HZ_PER_SEC = 2.0        # Klipper caps the sweep rate here
STATE = os.path.expanduser('~/printer_data/config/chopper-autotune/belts.json')


def gap_pct(freq_a: float, freq_b: float) -> float:
    return abs(freq_a - freq_b) / ((freq_a + freq_b) / 2) * 100


def load_state() -> 'dict | None':
    try:
        with open(STATE) as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_state(freq_a: float, freq_b: float):
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, 'w') as handle:
            json.dump({'A': freq_a, 'B': freq_b}, handle)
    except OSError:
        pass


def progress_message(freq_a: float, freq_b: float, prev: 'dict | None',
                     tolerance: float = MATCH_TOLERANCE) -> str:
    """A one-line status for the display: the gap now, how it changed since the previous run
    (per belt), and which belt to tighten — so you can gauge how much more to turn."""
    gap = gap_pct(freq_a, freq_b)
    now = 'A %.0f / B %.0f Hz' % (freq_a, freq_b)
    if prev and 'A' in prev and 'B' in prev:
        change = 'gap %.1f->%.1f%% (A%+.0f B%+.0f)' % (
            gap_pct(prev['A'], prev['B']), gap, freq_a - prev['A'], freq_b - prev['B'])
    else:
        change = 'gap %.1f%%' % gap
    if gap < tolerance:
        return 'Belts matched · %s · %s' % (change, now)
    looser = 'A' if freq_a < freq_b else 'B'
    return 'Tighten %s · %s · %s' % (looser, change, now)


def capture_span(path: str) -> float:
    """Seconds of data in a Klipper raw-accel CSV, reading only the file's edges (the
    last line may still be mid-write — skipped defensively)."""
    first = last = None
    with open(path, 'rb') as fh:
        for line in fh:
            if not line.startswith(b'#') and b',' in line:
                first = float(line.split(b',', 1)[0])
                break
        fh.seek(0, os.SEEK_END)
        fh.seek(-min(fh.tell(), 4096), os.SEEK_END)
        for line in reversed(fh.read().splitlines()):
            try:
                last = float(line.split(b',', 1)[0])
                break
            except (ValueError, IndexError):
                continue
    return (last - first) if first is not None and last is not None else 0.0


def wait_for_capture(pattern: str, min_span_sec: float = 0.0, timeout: float = 30.0) -> str:
    """Wait for Klipper's raw-accel CSV to appear and finish flushing. TEST_RESONANCES
    returns before its background writer has flushed the file, and the writer pauses
    between batches — a size that merely stopped growing for one poll can still be a
    TRUNCATED sweep (measured: a cut at ~60 s read as a phantom 156 Hz peak). So demand
    the size stay stable across two polls AND the capture cover the sweep duration."""
    deadline = time.time() + timeout
    last, stable = -1, 0
    path = None
    while time.time() < deadline:
        files = glob.glob(pattern)
        if files:
            path = max(files, key=os.path.getmtime)
            size = os.path.getsize(path)
            stable = stable + 1 if size > 0 and size == last else 0
            last = size
            if stable >= 2 and capture_span(path) >= 0.9 * min_span_sec:
                return path
        time.sleep(0.3)
    raise SystemExit('capture incomplete for %s: %.0fs of the %.0fs sweep flushed — '
                     'check the [resonance_tester] output path'
                     % (pattern, capture_span(path) if path else 0.0, min_span_sec))


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
    mean = (freq_a + freq_b) / 2
    apart = abs(freq_a - freq_b) / mean * 100
    if apart < tolerance:
        return 'balanced: %.1f%% apart (< %.0f%%) — the belts are matched.' % (apart, tolerance)
    looser = 'A' if freq_a < freq_b else 'B'
    slack = ((max(freq_a, freq_b) / min(freq_a, freq_b)) ** 2 - 1) * 100
    return ('MISMATCH: %.1f%% apart — belt %s resonates lower, so it is looser (~%.0f%% less '
            'tension). Tighten belt %s a little and re-run.' % (apart, looser, slack, looser))


def identify_belt(kl: Klippy, hw, motor: str, screen: Screen, cycles: int = 4):
    """Jog the head along this belt's diagonal so ONLY its loop moves — on CoreXY a 1,-1
    move is pure motor B, leaving belt A still — so the user can see which belt to adjust —
    then release the gantry motors so the belt is easy to reach and tension."""
    cx, cy = hw.center
    vec = stress_vector(hw.kinematics, motor)
    span = min(40.0, hw.axis_span / 6)
    label = motor_label(motor)
    print('Jogging belt %s so you can see which one to tighten, then releasing the motors...' % label)
    screen.update('Tighten belt %s — the moving one' % label, force=True)
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

    if args.show:                                   # just point at a belt, no measurement
        screen = Screen(kl, hw.display)
        refuse_if_printing(kl)
        motor = 'x' if args.show == 'a' else 'y'
        kl.gcode('G28 X Y\nM400')
        identify_belt(kl, hw, motor, screen)
        screen.update('Motors off — tighten belt %s, then re-measure' % motor_label(motor), force=True)
        return 0                                     # leaves the motors off on purpose

    tester = settings.get('resonance_tester') or {}
    if not tester.get('probe_points'):
        raise SystemExit('needs a [resonance_tester] with probe_points (same as Klipper '
                         'input-shaper calibration) — TEST_RESONANCES drives the excitation')

    band = (float(args.min_freq), float(args.max_freq))
    hz_per_sec = min(args.hz_per_sec, MAX_HZ_PER_SEC)   # Klipper rejects a faster sweep
    if hz_per_sec < args.hz_per_sec:
        print('Capping sweep rate to %g Hz/s (Klipper maximum)' % MAX_HZ_PER_SEC)
    print('Belt-tension match on %s: swept-sine %g-%g Hz per belt (motor A = head 1,1, '
          'motor B = head 1,-1), response spectrum computed here.'
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

    prev = load_state()                             # the previous run, to show what changed
    save_state(peaks['A'], peaks['B'])
    print('\n=== Belt-tension match ===')
    print('Belt A %.1f Hz  |  Belt B %.1f Hz' % (peaks['A'], peaks['B']))
    print(verdict(peaks['A'], peaks['B'], args.tolerance))
    if prev and 'A' in prev and 'B' in prev:
        print('Since last run: gap %.1f%% -> %.1f%%  (A %+.1f, B %+.1f Hz)'
              % (gap_pct(prev['A'], prev['B']), gap_pct(peaks['A'], peaks['B']),
                 peaks['A'] - prev['A'], peaks['B'] - prev['B']))

    message = progress_message(peaks['A'], peaks['B'], prev, args.tolerance)
    if gap_pct(peaks['A'], peaks['B']) >= args.tolerance:
        # release the gantry so the belt is easy to reach; the message names which one,
        # and the Motor A/B buttons (SHOW=) point at a motor when needed
        kl.gcode('SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=0\n'
                 'SET_STEPPER_ENABLE STEPPER=stepper_y ENABLE=0')
        message += ' · motors off'
    screen.update(message, force=True)
    print('\nBelt resonance goes as sqrt(tension); match the two, then re-run CHOPPER_TUNE — '
          'the per-motor chopper optimum is measured against the mechanics you leave in place.')
    return 0
