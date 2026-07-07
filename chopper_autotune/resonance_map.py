"""Resonance map: vibration versus speed, on the registers you actually print with.

Where `find-speed` scans on stock registers to expose the peak it needs for tuning,
this scans on the CURRENT (tuned) registers — the vibration you actually feel — over a
wide range, and reports which speeds ring (avoid cruising steadily there) versus which
are quiet.

Honesty first: a constant-velocity accelerometer sweep measures the motor/chopper
vibration signature, NOT your top print speed. The real speed ceiling is the hotend's
flow rate (thermal, ~10-15 mm3/s stock) and, for accel-driven ringing, the input shaper
(SHAPER_CALIBRATE). The motor's torque ceiling — measured separately by CHOPPER_ENVELOPE —
sits far above any speed the machine commands. So this map answers "where is it quiet?",
not "how fast should I print?".
"""
from __future__ import annotations

import time
from datetime import datetime
from pathlib import Path

from . import __version__, tmc
from .collect import (MOVE_MARGIN, OVERHEAD_CSV_SEC, OVERHEAD_STREAM_SEC, Screen,
                      default_dataset_root, detect_hardware, enter_spreadcycle, exit_spreadcycle,
                      make_parker, now, park, refuse_if_printing, run_restore)
from .dataset import Dataset
from .find_speed import build_curve, build_speed_plan, find_peaks, run_sweep, smooth, write_report
from .klippy import Klippy, find_socket

QUIET_RATIO = 1.25          # a speed is "quiet" if its vibration is within this of the floor
BAR_WIDTH = 32


def quiet_band(curve: 'list[tuple[int, float]]', ratio: float = QUIET_RATIO) -> 'tuple[int, int] | None':
    """Widest contiguous speed span whose vibration stays within `ratio` of the minimum."""
    if not curve:
        return None
    floor = min(magnitude for _, magnitude in curve)
    threshold = floor * ratio
    best = run = None
    for speed, magnitude in curve:
        run = (run[0] if run else speed, speed) if magnitude <= threshold else None
        if run and (best is None or run[1] - run[0] > best[1] - best[0]):
            best = run
    return best


def render_map(curve: 'list[tuple[int, float]]', peaks: 'list[int]',
               quiet: 'tuple[int, int] | None') -> 'list[str]':
    """A speed/vibration table with bars; peaks flagged 'ring', the quiet band flagged."""
    ceiling = max(magnitude for _, magnitude in curve)
    peak_speeds = {curve[i][0] for i in peaks}
    lines = ['  speed   vibration']
    for speed, magnitude in curve:
        bar = '#' * max(1, round(magnitude / ceiling * BAR_WIDTH))
        tag = '  <- resonance (avoid cruising here)' if speed in peak_speeds else ''
        if not tag and quiet and quiet[0] <= speed <= quiet[1]:
            tag = '  quiet'
        lines.append('  %4d   %-*s %5.0f%s' % (speed, BAR_WIDTH, bar, magnitude, tag))
    return lines


def run_resonance_map(args) -> int:
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        return resonance_map(kl, args)
    finally:
        kl.close()


def resonance_map(kl: Klippy, args) -> int:
    args.source = 'csv' if args.csv else 'stream'
    if args.trim is None:
        args.trim = 0.25 if args.csv else 0.1

    hw = detect_hardware(kl, args.axis)
    print('Driver tmc%s on %s (motor %s), accelerometer %s, kinematics %s, current registers %s'
          % (hw.driver.name, hw.stepper, hw.motor, hw.accel_chip, hw.kinematics, hw.baseline))

    accel = args.accel or hw.max_accel / 4          # higher than find-speed: reach print speeds
    limit = hw.axis_span * MOVE_MARGIN
    plan = build_speed_plan(args, accel, limit)

    n_moves = len(plan) * args.iterations * 2
    overhead = OVERHEAD_CSV_SEC if args.csv else OVERHEAD_STREAM_SEC
    eta = sum(2 * (cruise + 2 * speed / accel + overhead) * args.iterations for speed, cruise in plan)
    print('Resonance map on motor %s at the CURRENT registers (what you print with), '
          'accel %.0f' % (hw.motor, accel))
    print('Plan: %d speeds (%d..%d step %d) -> %d moves, capture %s, ETA %dm %02ds'
          % (len(plan), plan[0][0], plan[-1][0], args.step, n_moves, args.source, eta // 60, eta % 60))
    if args.dry_run:
        return 0
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1

    refuse_if_printing(kl)
    if not args.csv:
        kl.subscribe_accel(hw.accel_chip)

    root = Path(args.dataset) if args.dataset else default_dataset_root(
        '%s_map_%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'), args.axis))
    ds = Dataset.create(root, {
        'version': __version__,
        'created': now(),
        'mode': 'resonance-map',
        'klippy_socket': kl.path,
        'capture': args.source,
        'axis': args.axis,
        'stepper': hw.stepper,
        'driver': hw.driver.name,
        'accel_chip': hw.accel_chip,
        'kinematics': hw.kinematics,
        'registers': hw.baseline,
        'accel': accel,
        'measure_time': args.measure_time,
        'trim': args.trim,
        'iterations': args.iterations,
        'speeds': [speed for speed, _ in plan],
    })
    done = ds.done_ids()
    if done:
        print('Resuming %s: %d measurements already present' % (root, len(done)))

    print('Preparing: home XY, park at center, disable motors')
    park(kl, hw)
    enter_spreadcycle(kl, hw)                        # measure in spreadCycle; registers untouched
    started = time.time()
    screen = Screen(kl, hw.display)
    before_move = make_parker(kl, hw)
    try:
        failed = run_sweep(hw, ds, args, plan, accel, screen, before_move, done)
    finally:
        print('Homing')
        run_restore(lambda: exit_spreadcycle(kl, hw), lambda: kl.gcode('G28 X Y'))

    curve = build_curve(ds)
    if not curve:
        raise SystemExit('no successful measurements')
    peaks = find_peaks(smooth([magnitude for _, magnitude in curve]))
    quiet = quiet_band(curve)

    path = str(root / 'report.html')
    write_report(curve, peaks, 'resonance map: motor %s (%s), current registers %s'
                 % (hw.motor, hw.stepper, hw.baseline), path)

    print('\n=== Resonance map: motor %s @ current registers ===' % hw.motor)
    print('\n'.join(render_map(curve, peaks, quiet)))
    if peaks:
        print('\nResonance peaks (avoid steady cruising here): %s'
              % ', '.join('%d mm/s' % curve[i][0] for i in peaks))
    if quiet:
        print('Quietest band: %d-%d mm/s' % quiet)
    print('\nThis is the motor/chopper vibration signature vs speed — not your top print '
          'speed. The motor holds torque far past any commanded speed (run CHOPPER_ENVELOPE); '
          'the real ceiling is hotend flow, and accel-driven ringing is the input shaper '
          '(SHAPER_CALIBRATE). Use this to avoid cruising on a resonance, not to pick a limit.')
    print('Report: %s (done in %dm)' % (path, (time.time() - started) // 60))
    return 0 if failed == 0 else 2
