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

import json
import os
import time
from datetime import datetime
from pathlib import Path

from . import __version__, tmc
from .collect import (MOVE_MARGIN, OVERHEAD_CSV_SEC, OVERHEAD_STREAM_SEC, Screen,
                      default_dataset_root, detect_hardware, enter_spreadcycle, exit_spreadcycle,
                      make_parker, now, park, refuse_if_printing, run_restore)
from .dataset import Dataset
from .find_speed import (build_curve, build_speed_plan, find_peaks, find_valleys, run_sweep,
                         smooth, write_report)
from .klippy import Klippy, find_socket

BAR_WIDTH = 32
NEAR_WINDOW = 40            # mm/s around a target speed to look for a quieter alternative
QUIETER_MARGIN = 0.05       # an alternative must be at least this much quieter to bother suggesting
STATE = os.path.expanduser('~/printer_data/config/chopper-autotune/map.json')


def save_state(motor: str, peaks: 'list[int]', dips: 'list[int]', advice: 'str | None'):
    """Remember the map per motor so the panel's Results can show where it rings."""
    try:
        state = {}
        try:
            with open(STATE) as handle:
                state = json.load(handle)
        except (OSError, ValueError):
            pass
        state[motor] = {'peaks': peaks, 'dips': dips, 'advice': advice}
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, 'w') as handle:
            json.dump(state, handle)
    except OSError:
        pass


def render_map(curve: 'list[tuple[int, float]]', peaks: 'list[int]',
               valleys: 'list[int]') -> 'list[str]':
    """A speed/vibration table with bars; resonance peaks flagged (VFA risk), quiet dips flagged."""
    ceiling = max(magnitude for _, magnitude in curve)
    peak_speeds = {curve[i][0] for i in peaks}
    valley_speeds = {curve[i][0] for i in valleys}
    lines = ['  speed   vibration']
    for speed, magnitude in curve:
        bar = '#' * max(1, round(magnitude / ceiling * BAR_WIDTH))
        tag = ''
        if speed in peak_speeds:
            tag = '  <- resonance (VFA risk — avoid cruising)'
        elif speed in valley_speeds:
            tag = '  <- quiet dip'
        lines.append('  %4d   %-*s %5.0f%s' % (speed, BAR_WIDTH, bar, magnitude, tag))
    return lines


def quieter_alternatives(curve: 'list[tuple[int, float]]', target: int,
                         window: int = NEAR_WINDOW, margin: float = QUIETER_MARGIN):
    """For a target print speed, return (at, below, above): the nearest sampled point and the
    quietest speed within `window` on each side that runs at least `margin` quieter. Each is a
    (speed, magnitude) pair or None. Answers "is my print speed on a bump, and what's quieter?"."""
    at = min(curve, key=lambda point: abs(point[0] - target))
    limit = at[1] * (1 - margin)
    below = [p for p in curve if at[0] - window <= p[0] < at[0] and p[1] <= limit]
    above = [p for p in curve if at[0] < p[0] <= at[0] + window and p[1] <= limit]
    best = lambda points: min(points, key=lambda p: p[1]) if points else None
    return at, best(below), best(above)


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
    smoothed = smooth([magnitude for _, magnitude in curve])
    peaks = find_peaks(smoothed)
    valleys = find_valleys(smoothed)

    path = str(root / 'report.html')
    write_report(curve, peaks, 'resonance map: motor %s (%s), current registers %s'
                 % (hw.motor, hw.stepper, hw.baseline), path)

    print('\n=== Resonance map: motor %s @ current registers ===' % hw.motor)
    print('\n'.join(render_map(curve, peaks, valleys)))
    if peaks:
        print('\nResonance peaks (cause VFAs — avoid steady cruising here): %s'
              % ', '.join('%d mm/s' % curve[i][0] for i in peaks))
    if valleys:
        print('Quiet cruise speeds (dips between resonances): %s'
              % ', '.join('%d mm/s' % curve[i][0] for i in valleys))
    advice = None
    if args.print_speed:
        at, below, above = quieter_alternatives(curve, args.print_speed)
        alts = ['%d mm/s (%.0f, %+.0f%%)' % (s, m, (m / at[1] - 1) * 100)
                for s, m in (below, above) if s is not None]
        if alts:
            print('\nYour print speed %d mm/s measures %.0f — quieter nearby: %s'
                  % (args.print_speed, at[1], ', '.join(alts)))
            advice = '%d→%s' % (args.print_speed,
                                '/'.join(str(s) for s, _ in (below, above) if s is not None))
        else:
            print('\nYour print speed %d mm/s (%.0f) is already in a quiet spot — no nearby '
                  'speed runs meaningfully quieter.' % (args.print_speed, at[1]))
            advice = '%d ok' % args.print_speed
    peak_speeds = [curve[i][0] for i in peaks]
    dip_speeds = [curve[i][0] for i in valleys]
    save_state(hw.motor, peak_speeds, dip_speeds, advice)   # the panel's Results shows these
    screen.final('Map %s: peaks %s · dips %s%s' % (
        hw.motor,
        ','.join(str(s) for s in peak_speeds) or '—',
        ','.join(str(s) for s in dip_speeds) or '—',
        ' · %s' % advice if advice else ''))
    print('\nVFAs (fine vertical banding) come from cruising on a motor resonance — that is what '
          'this map catches, so avoid the peaks above. It is NOT your top print speed: the motor '
          'holds torque far past any commanded speed (CHOPPER_ENVELOPE), the real ceiling is '
          'hotend flow, and corner ringing is the input shaper (SHAPER_CALIBRATE).')
    print('Report: %s (done in %dm)' % (path, (time.time() - started) // 60))
    return 0 if failed == 0 else 2
