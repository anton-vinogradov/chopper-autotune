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
import os
import time

import numpy as np

from .collect import Screen, coupled_xy, detect_hardware, motor_label, refuse_if_printing, run_restore
from .current import stress_vector
from .klippy import Klippy, find_socket

SEGMENT = 1024              # Welch window; ~0.3 s at the ADXL's ~3.2 kHz -> ~3 Hz resolution
MATCH_TOLERANCE = 5.0       # percent apart below which the belts count as matched
MAX_HZ_PER_SEC = 2.0        # Klipper caps the sweep rate here


def wait_for_capture(pattern: str, timeout: float = 20.0) -> str:
    """Wait for Klipper's raw-accel CSV to appear and finish flushing. TEST_RESONANCES
    returns before its background writer has flushed the file (with OUTPUT=resonances the
    PSD step masked this; with raw_data alone we would read an empty file), so poll until
    a matching file exists and its size stops growing."""
    deadline = time.time() + timeout
    last = -1
    path = None
    while time.time() < deadline:
        files = glob.glob(pattern)
        if files:
            path = max(files, key=os.path.getmtime)
            size = os.path.getsize(path)
            if size > 0 and size == last:
                return path
            last = size
        time.sleep(0.3)
    if path and os.path.getsize(path) > 0:
        return path
    raise SystemExit('TEST_RESONANCES produced no usable capture (%s) — check the '
                     '[resonance_tester] output path' % pattern)


def welch_peak(path: str, band: 'tuple[float, float]') -> 'tuple[float, float]':
    """Dominant response frequency within `band` from a Klipper raw-accel CSV, via a Welch
    PSD (segmented, Hann-windowed, summed over axes). Returns (peak Hz, bin width Hz)."""
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
    psd /= segments
    freqs = np.fft.rfftfreq(SEGMENT, 1 / fs)
    mask = (freqs >= band[0]) & (freqs <= band[1])
    return float(freqs[mask][np.argmax(psd[mask])]), float(freqs[1] - freqs[0])


def verdict(freq_a: float, freq_b: float, tolerance: float = MATCH_TOLERANCE) -> str:
    mean = (freq_a + freq_b) / 2
    apart = abs(freq_a - freq_b) / mean * 100
    if apart < tolerance:
        return 'balanced: %.1f%% apart (< %.0f%%) — the belts are matched.' % (apart, tolerance)
    looser = 'A' if freq_a < freq_b else 'B'
    slack = ((max(freq_a, freq_b) / min(freq_a, freq_b)) ** 2 - 1) * 100
    return ('MISMATCH: %.1f%% apart — belt %s resonates lower, so it is looser (~%.0f%% less '
            'tension). Tighten belt %s a little and re-run.' % (apart, looser, slack, looser))


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
            path = wait_for_capture('/tmp/raw_data_*belt%s*.csv' % label)
            peak, binwidth = welch_peak(path, band)
            peaks[label] = peak
            edge = '  (near the sweep edge — raise MAX_FREQ)' if peak >= band[1] - 2 * binwidth else ''
            print('   belt %s: resonance %.1f Hz%s' % (label, peak, edge))
    finally:
        run_restore(lambda: kl.gcode('G28 X Y'))

    print('\n=== Belt-tension match ===')
    print('Belt A %.1f Hz  |  Belt B %.1f Hz' % (peaks['A'], peaks['B']))
    print(verdict(peaks['A'], peaks['B'], args.tolerance))
    screen.update('Belts A %.0f / B %.0f Hz' % (peaks['A'], peaks['B']), force=True)
    print('\nBelt resonance goes as sqrt(tension); match the two, then re-run CHOPPER_TUNE — '
          'the per-motor chopper optimum is measured against the mechanics you leave in place.')
    return 0
