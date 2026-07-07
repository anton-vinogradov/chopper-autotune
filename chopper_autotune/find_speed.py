"""Resonance speed scan: sweep speeds with stock registers, locate vibration peaks."""
from __future__ import annotations

import statistics
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from . import __version__, tmc
from .collect import (MOVE_MARGIN, OVERHEAD_CSV_SEC, OVERHEAD_STREAM_SEC, Screen,
                      default_dataset_root, detect_hardware, enter_spreadcycle,
                      exit_spreadcycle, make_parker, measure_baseline, measure_move, now, park,
                      refuse_if_printing, run_restore, travel_for)
from .dataset import Dataset
from .klippy import Klippy, find_socket

MIN_CRUISE_SEC = 0.25


def cruise_for(speed: float, accel: float, travel_limit: float, requested: float) -> float:
    """Cruise time that keeps the whole move within the travel limit."""
    available = (travel_limit - speed * speed / accel) / speed
    return min(requested, available)


def smooth(values: 'list[float]') -> 'list[float]':
    if len(values) < 3:
        return list(values)
    inner = [(values[i - 1] + values[i] + values[i + 1]) / 3 for i in range(1, len(values) - 1)]
    return [values[0]] + inner + [values[-1]]


def find_peaks(magnitudes: 'list[float]', prominence_ratio: float = 0.15) -> 'list[int]':
    """Indices of local maxima whose prominence exceeds the given share of the curve span."""
    n = len(magnitudes)
    if n < 3:
        return []
    span = max(magnitudes) - min(magnitudes)
    if span <= 0:
        return []
    peaks = []
    for i in range(1, n - 1):
        if not magnitudes[i - 1] <= magnitudes[i] > magnitudes[i + 1]:
            continue
        left_min = right_min = magnitudes[i]
        for j in range(i - 1, -1, -1):
            if magnitudes[j] > magnitudes[i]:
                break
            left_min = min(left_min, magnitudes[j])
        for j in range(i + 1, n):
            if magnitudes[j] > magnitudes[i]:
                break
            right_min = min(right_min, magnitudes[j])
        if magnitudes[i] - max(left_min, right_min) >= prominence_ratio * span:
            peaks.append(i)
    return peaks


def recommend(curve: 'list[tuple[int, float]]', peaks: 'list[int]') -> 'int | None':
    """Lowest peak speed that is at least half as strong as the strongest peak.

    A weak low-speed hump is a worse tuning point than the dominant resonance:
    register differences drown in noise there.
    """
    if not peaks:
        return None
    strongest = max(curve[i][1] for i in peaks)
    for i in peaks:
        if curve[i][1] >= 0.5 * strongest:
            return curve[i][0]
    return None


def scan_id(speed: int, iteration: int, direction: int) -> str:
    return 'v%03d_i%d_%s' % (speed, iteration, 'fwd' if direction > 0 else 'rev')


def build_speed_plan(args, accel: float, limit: float) -> 'list[tuple[int, float]]':
    """(speed, cruise) pairs whose whole move fits the travel limit, over the requested range."""
    plan = []
    for speed in range(args.min_speed, args.max_speed + 1, args.step):
        cruise = cruise_for(speed, accel, limit, args.measure_time)
        if cruise >= MIN_CRUISE_SEC:
            plan.append((speed, cruise))
    if not plan:
        raise SystemExit('no speeds fit into %.0fmm travel; lower --min-speed or raise --accel' % limit)
    if plan[-1][0] < args.max_speed:
        print('Warning: speeds above %d mm/s skipped, cruise would drop below %.2fs within %.0fmm'
              % (plan[-1][0], MIN_CRUISE_SEC, limit))
    return plan


def run_sweep(hw, ds: Dataset, args, plan: 'list[tuple[int, float]]', accel: float,
              screen: Screen, before_move, done: set) -> int:
    """Measure vibration at every planned speed, both directions, into ds; return the failed
    count. Whatever chopper registers the caller left in effect are what gets measured."""
    failed = 0
    measure_baseline(hw, ds, args, done)
    for index, (speed, cruise) in enumerate(plan, 1):
        travel = travel_for(speed, accel, cruise)
        magnitudes = []
        for iteration in range(args.iterations):
            for direction in (1, -1):
                mid = scan_id(speed, iteration, direction)
                if mid in done:
                    continue
                record = {'id': mid, 'kind': 'speed', 'source': args.source, 'speed': speed,
                          'cruise': round(cruise, 3), 'direction': direction,
                          'iteration': iteration, 'ts': now()}
                measure_move(hw, ds, args, record, speed, cruise, travel, direction, accel,
                             before_move)
                if record['status'] == 'ok':
                    magnitudes.append(record['score']['median_magnitude'])
                else:
                    failed += 1
        if magnitudes:
            print('[%d/%d] %d mm/s: median %.1f' % (index, len(plan), speed,
                                                    sum(magnitudes) / len(magnitudes)))
        screen.update('Chopper speed scan %d/%d' % (index, len(plan)))
    return failed


def build_curve(ds: Dataset) -> 'list[tuple[int, float]]':
    by_speed = defaultdict(list)
    for record in ds.records():
        if record.get('kind') == 'speed' and record.get('status') == 'ok':
            by_speed[record['speed']].append(record['score']['median_magnitude'])
    return [(speed, statistics.median(values)) for speed, values in sorted(by_speed.items())]


def write_report(curve: 'list[tuple[int, float]]', peaks: 'list[int]', title: str, path: str):
    import plotly.graph_objects as go
    speeds = [speed for speed, _ in curve]
    magnitudes = [magnitude for _, magnitude in curve]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=speeds, y=magnitudes, mode='lines+markers', name='median magnitude'))
    fig.add_trace(go.Scatter(x=[speeds[i] for i in peaks], y=[magnitudes[i] for i in peaks],
                             mode='markers', marker={'size': 12, 'color': '#d62728'},
                             name='resonance peaks'))
    fig.update_layout(title=title, xaxis_title='speed, mm/s', yaxis_title='median magnitude')
    fig.write_html(path)


def run_find_speed(args) -> int:
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        code, _ = scan(kl, args)
        return code
    finally:
        kl.close()


def scan(kl: Klippy, args) -> 'tuple[int, int | None]':
    args.source = 'csv' if args.csv else 'stream'
    if args.trim is None:
        args.trim = 0.25 if args.csv else 0.1

    hw = detect_hardware(kl, args.axis)
    print('Driver tmc%s on %s (motor %s), accelerometer %s, kinematics %s, registers %s'
          % (hw.driver.name, hw.stepper, hw.motor, hw.accel_chip, hw.kinematics, hw.baseline))

    accel = args.accel or hw.max_accel / 10
    limit = hw.axis_span * MOVE_MARGIN
    plan = build_speed_plan(args, accel, limit)

    n_moves = len(plan) * args.iterations * 2
    overhead = OVERHEAD_CSV_SEC if args.csv else OVERHEAD_STREAM_SEC
    eta = sum(2 * (cruise + 2 * speed / accel + overhead) * args.iterations for speed, cruise in plan)
    print('Plan: %d speeds (%d..%d step %d) -> %d moves, capture %s, ETA %dm %02ds'
          % (len(plan), plan[0][0], plan[-1][0], args.step, n_moves, args.source,
             eta // 60, eta % 60))
    if args.dry_run:
        return 0, None
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1, None

    refuse_if_printing(kl)
    if not args.csv:
        kl.subscribe_accel(hw.accel_chip)

    root = Path(args.dataset) if args.dataset else default_dataset_root(
        '%s_speed_%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'), args.axis))
    ds = Dataset.create(root, {
        'version': __version__,
        'created': now(),
        'mode': 'find-speed',
        'klippy_socket': kl.path,
        'capture': args.source,
        'axis': args.axis,
        'stepper': hw.stepper,
        'driver': hw.driver.name,
        'accel_chip': hw.accel_chip,
        'kinematics': hw.kinematics,
        'registers': hw.baseline,
        'scan_registers': tmc.KLIPPER_DEFAULT.fields(),
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
    enter_spreadcycle(kl, hw)
    # scan with the stock chopper: a well-tuned config suppresses the very resonance
    # peaks the scan is looking for (measured: 897 vs 2676 at the same speed)
    kl.gcode(tmc.set_fields_script(hw.stepper, tmc.KLIPPER_DEFAULT.fields()))
    print('Scanning with Klipper default registers %s — the current tuning would mask the peaks'
          % tmc.KLIPPER_DEFAULT.label())
    started = time.time()
    screen = Screen(kl, hw.display)
    before_move = make_parker(kl, hw)
    try:
        failed = run_sweep(hw, ds, args, plan, accel, screen, before_move, done)
    finally:
        print('Restoring registers, homing')
        run_restore(
            lambda: hw.baseline and kl.gcode(tmc.set_fields_script(hw.stepper, hw.baseline)),
            lambda: exit_spreadcycle(kl, hw),
            lambda: kl.gcode('G28 X Y'))

    curve = build_curve(ds)
    if not curve:
        raise SystemExit('no successful measurements')
    peaks = find_peaks(smooth([magnitude for _, magnitude in curve]))

    path = str(root / 'report.html')
    write_report(curve, peaks, 'resonance scan: motor %s (%s), %s'
                 % (hw.motor, hw.stepper, args.source), path)
    print('Done in %dm: report %s' % ((time.time() - started) // 60, path))

    recommended = None
    if peaks:
        recommended = recommend(curve, peaks)
        print('Resonance peaks: %s'
              % ', '.join('%d mm/s (magnitude %.0f)' % curve[i] for i in peaks))
        print('\nRecommended: CHOPPER_COLLECT MOTOR=%s SPEED=%d' % (hw.motor, recommended))
        screen.update('Chopper: resonance %d mm/s' % recommended, force=True)
    else:
        top = max(curve, key=lambda point: point[1])
        print('No clear resonance peaks; highest magnitude %.0f at %d mm/s. '
              'Consider widening the range or more --iterations.' % (top[1], top[0]))
    return (0 if failed == 0 else 2), recommended
