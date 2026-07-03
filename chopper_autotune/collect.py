"""Collection phase: drive the printer over a register/speed grid, record a dataset.

Runs on the printer host: talks to the klippy unix socket directly and streams
accelerometer samples over it; CSV files in /tmp are the fallback path (--csv).
"""
from __future__ import annotations

import glob
import itertools
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from . import __version__, tmc
from .dataset import Dataset, RESULTS_HOME
from .klippy import Klippy, KlippyError, find_socket
from .metrics import parse_accel_csv, vibration_score, window

CSV_WAIT_SEC = 30.0
MOVE_MARGIN = 0.4
MIN_STEADY_SAMPLES = 32
PARK_INTERVAL_MOVES = 400
OVERHEAD_STREAM_SEC = 0.3
OVERHEAD_CSV_SEC = 3.0
VALIDATE_TOP = 3


@dataclass(frozen=True)
class Range:
    lo: int
    hi: int

    @classmethod
    def parse(cls, text: str) -> 'Range':
        lo, _, hi = text.partition(':')
        r = cls(int(lo), int(hi or lo))
        if r.hi < r.lo:
            raise ValueError('range %s: max < min' % text)
        return r

    def values(self) -> range:
        return range(self.lo, self.hi + 1)


@dataclass
class Hardware:
    kl: Klippy
    stepper: str
    driver: tmc.Driver
    accel_chip: str
    kinematics: str
    axis_span: float
    center: 'tuple[float, float]'
    max_accel: float
    baseline: 'dict[str, int]'


def detect_hardware(kl: Klippy, axis: str) -> Hardware:
    settings = kl.settings()
    stepper = 'stepper_' + axis
    driver = section = None
    for name in tmc.DRIVERS:
        candidate = 'tmc%s %s' % (name, stepper)
        if candidate in settings:
            driver, section = tmc.DRIVERS[name], settings[candidate]
            break
    if driver is None:
        raise SystemExit('no supported TMC driver section found for %s' % stepper)

    baseline = {}
    for field in ('tbl', 'toff', 'hstrt', 'hend') + (('tpfd',) if driver.has_tpfd else ()):
        value = section.get('driver_' + field)
        if value is not None:
            baseline[field] = int(value)

    spans, centers = {}, {}
    for ax in ('x', 'y'):
        rail = settings.get('stepper_' + ax)
        if rail is None or rail.get('position_max') is None:
            raise SystemExit('unsupported kinematics: no stepper_%s with position_max' % ax)
        lo, hi = float(rail.get('position_min', 0.0)), float(rail['position_max'])
        spans[ax], centers[ax] = hi - lo, (lo + hi) / 2

    kinematics = settings['printer']['kinematics']
    span = min(spans.values()) if 'core' in kinematics or 'hbot' in kinematics else spans[axis]

    resonance = settings.get('resonance_tester') or {}
    return Hardware(
        kl=kl,
        stepper=stepper,
        driver=driver,
        accel_chip=resonance.get('accel_chip', 'adxl345'),
        kinematics=kinematics,
        axis_span=span,
        center=(centers['x'], centers['y']),
        max_accel=float(settings['printer']['max_accel']),
        baseline=baseline,
    )


def build_plan(driver: tmc.Driver, tbl: Range, toff: Range, hstrt: Range, hend: Range,
               tpfd: Optional[Range], speeds: 'list[int]',
               skip_audible: bool = False) -> 'list[tuple[tmc.Chopper, int]]':
    tpfd_values = list(tpfd.values()) if tpfd is not None and driver.has_tpfd else [None]
    plan = []
    for t, o, hs, he, tp in itertools.product(tbl.values(), toff.values(), hstrt.values(),
                                              hend.values(), tpfd_values):
        combo = tmc.Chopper(t, o, hs, he, tp)
        if tmc.validate(combo) is not None:
            continue
        if skip_audible and tmc.is_audible(combo, driver):
            continue
        plan.extend((combo, speed) for speed in speeds)
    return plan


def travel_for(speed: float, accel: float, measure_time: float) -> float:
    return speed * speed / accel + speed * measure_time


def steady_window(t_end: float, speed: float, accel: float, measure_time: float,
                  guard_fraction: float) -> 'tuple[float, float]':
    """Exact cruise-phase bounds of a trapezoidal move that finished at print time t_end."""
    accel_time = speed / accel
    guard = guard_fraction * measure_time
    return t_end - accel_time - measure_time + guard, t_end - accel_time - guard


def default_dataset_root(stamp: str) -> Path:
    """Under printer_data when present, so Mainsail/Fluidd file manager shows the results."""
    base = RESULTS_HOME / 'datasets' if RESULTS_HOME.parent.is_dir() else Path('datasets')
    return base / stamp


def measurement_id(combo: tmc.Chopper, speed: int, iteration: int, direction: int) -> str:
    return '%s_v%d_i%d_%s' % (combo.label(), speed, iteration, 'fwd' if direction > 0 else 'rev')


def wait_for_csv(name: str, timeout: float = CSV_WAIT_SEC) -> Path:
    """Klipper dumps the CSV asynchronously after the measurement stops; wait until the size settles."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        matches = glob.glob('/tmp/*-%s.csv' % name)
        if matches:
            path = Path(matches[0])
            size = path.stat().st_size
            time.sleep(0.3)
            if size > 0 and path.stat().st_size == size:
                return path
        time.sleep(0.2)
    raise TimeoutError('accelerometer csv for %s did not appear in /tmp' % name)


def drop_stale_csv(name: str):
    for stale in glob.glob('/tmp/*-%s.csv' % name):
        os.unlink(stale)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def park(kl: Klippy, hw: Hardware):
    kl.gcode('G28 X Y\nG0 X%.1f Y%.1f F6000\nM400\nM18' % hw.center)


def capture_stream(hw: Hardware, script: str, duration: float) -> 'tuple[float, np.ndarray]':
    hw.kl.gcode('M400')
    hw.kl.gcode(script + '\nM400')
    t_end = hw.kl.print_time()
    hw.kl.wait_for_sample(t_end)
    samples = hw.kl.samples_between(t_end - duration, t_end)
    if len(samples) < MIN_STEADY_SAMPLES:
        raise ValueError('only %d samples streamed for a %.2fs window' % (len(samples), duration))
    return t_end, np.array(samples, dtype=float)


def capture_csv(hw: Hardware, name: str, script: str) -> np.ndarray:
    drop_stale_csv(name)
    measure = 'ACCELEROMETER_MEASURE CHIP=%s NAME=%s' % (hw.accel_chip, name)
    hw.kl.gcode('\n'.join(['M400', measure, script, 'M400', measure]))
    csv_path = wait_for_csv(name)
    with open(csv_path) as f:
        data = parse_accel_csv(f)
    os.unlink(csv_path)
    return data


def measure_baseline(hw: Hardware, ds: Dataset, args, done: set):
    if 'baseline' in done:
        return
    record = {'id': 'baseline', 'kind': 'baseline', 'source': args.source, 'ts': now()}
    dwell = 'G4 P%d' % int(args.measure_time * 1000)
    if args.csv:
        data = capture_csv(hw, 'baseline', dwell)
    else:
        _, data = capture_stream(hw, dwell, args.measure_time)
    record['score'] = vibration_score(data, args.trim if args.csv else 0.0)
    if not args.no_raw:
        record['raw'] = ds.store_raw_samples('baseline', data)
    record['status'] = 'ok'
    ds.append(record)
    print('Baseline noise: median magnitude %.1f' % record['score']['median_magnitude'])


def measure_move(hw: Hardware, ds: Dataset, args, record: dict, speed: float, cruise: float,
                 travel: float, direction: int, accel: float) -> dict:
    """One FORCE_MOVE with capture and scoring; cruise is the steady-window duration."""
    move = 'FORCE_MOVE STEPPER=%s DISTANCE=%.3f VELOCITY=%.1f ACCEL=%.0f' \
           % (hw.stepper, travel * direction, speed, accel)
    for attempt in (1, 2):
        try:
            if args.csv:
                data = capture_csv(hw, record['id'], move)
                record['score'] = vibration_score(data, args.trim)
            else:
                overflows = hw.kl.overflows
                duration = travel / speed + speed / accel
                t_end, data = capture_stream(hw, move, duration)
                steady = steady_window(t_end, speed, accel, cruise, args.trim)
                sliced = window(data, *steady)
                if len(sliced) < MIN_STEADY_SAMPLES:
                    raise ValueError('only %d samples in the steady window' % len(sliced))
                record['steady'] = [round(steady[0], 6), round(steady[1], 6)]
                record['score'] = vibration_score(sliced, 0.0)
                lost = hw.kl.overflows - overflows
                if lost:
                    record['score']['overflows'] = lost
            if not args.no_raw:
                record['raw'] = ds.store_raw_samples(record['id'], data)
            record['status'] = 'ok'
            break
        except (KlippyError, TimeoutError, ValueError, OSError) as e:
            if attempt == 2:
                record['status'] = 'failed'
                record['error'] = str(e)
                print('  %s failed: %s' % (record['id'], e))
    ds.append(record)
    return record


def run_measurement(hw: Hardware, ds: Dataset, args, combo: tmc.Chopper, speed: int,
                    iteration: int, direction: int, travel: float, accel: float) -> dict:
    record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
              'source': args.source, **combo.fields(), 'speed': speed,
              'direction': direction, 'iteration': iteration, 'ts': now()}
    return measure_move(hw, ds, args, record, speed, args.measure_time, travel, direction, accel)


def make_parker(kl: Klippy, hw: Hardware):
    """Call before every move: re-homes periodically so retry drift cannot random-walk to a rail."""
    counter = {'moves': 0}

    def before_move():
        if counter['moves'] >= PARK_INTERVAL_MOVES:
            print('Re-homing to reset accumulated drift')
            park(kl, hw)
            counter['moves'] = 0
        counter['moves'] += 1
    return before_move


def run_grid(kl: Klippy, hw: Hardware, ds: Dataset, args, plan, travel: float, accel: float,
             done: set, before_move) -> 'tuple[int, int]':
    ok = failed = 0
    for index, (combo, speed) in enumerate(plan, 1):
        pending = [(i, d) for i in range(args.iterations) for d in (1, -1)
                   if measurement_id(combo, speed, i, d) not in done]
        if not pending:
            continue
        kl.gcode(tmc.set_fields_script(hw.stepper, combo.fields()))
        magnitudes = []
        for iteration, direction in pending:
            before_move()
            record = run_measurement(hw, ds, args, combo, speed, iteration, direction, travel, accel)
            if record['status'] == 'ok':
                ok += 1
                magnitudes.append(record['score']['median_magnitude'])
            else:
                failed += 1
        if magnitudes:
            print('[%d/%d] %s v%d: median %.1f' % (index, len(plan), combo.label(), speed,
                                                   sum(magnitudes) / len(magnitudes)))
    return ok, failed


def run_descent(kl: Klippy, hw: Hardware, ds: Dataset, args, tpfd: 'Range | None',
                speeds: 'list[int]', travel: float, accel: float, done: set,
                before_move) -> 'tuple[int, int]':
    from .analyze import aggregate, print_table, rank
    from .search import coordinate_descent, dataset_history, penalized_score, seed_start

    stats = {'ok': 0, 'failed': 0}
    history = dataset_history(ds)
    cache = {combo: penalized_score(combo, magnitudes, hw.driver, args.audible_weight)
             for combo, magnitudes in history.items()}
    if cache:
        print('Resuming: %d candidates already measured' % len(cache))

    def measure_candidate(combo: tmc.Chopper, iterations: int, first_iteration: int = 0):
        kl.gcode(tmc.set_fields_script(hw.stepper, combo.fields()))
        for speed in speeds:
            for iteration in range(first_iteration, first_iteration + iterations):
                for direction in (1, -1):
                    if measurement_id(combo, speed, iteration, direction) in done:
                        continue
                    before_move()
                    record = run_measurement(hw, ds, args, combo, speed, iteration, direction,
                                             travel, accel)
                    if record['status'] == 'ok':
                        stats['ok'] += 1
                        history[combo].append(record['score']['median_magnitude'])
                    else:
                        stats['failed'] += 1

    def evaluate(combo: tmc.Chopper) -> float:
        if combo in cache:
            return cache[combo]
        if args.skip_audible and tmc.is_audible(combo, hw.driver):
            cache[combo] = float('inf')
            return cache[combo]
        measure_candidate(combo, args.iterations)
        score = (penalized_score(combo, history[combo], hw.driver, args.audible_weight)
                 if history[combo] else float('inf'))
        cache[combo] = score
        note = ' audible' if tmc.is_audible(combo, hw.driver) else ''
        print('  %s -> %s' % (combo.label(),
                              'failed' if score == float('inf') else '%.1f%s' % (score, note)))
        return score

    if args.seed_from:
        start = seed_start(Dataset.open(args.seed_from), hw.driver, args.audible_weight)
        print('Seeded from %s: starting at %s' % (args.seed_from, start.label()))
    else:
        start = tmc.Chopper(hw.baseline.get('tbl', 2), hw.baseline.get('toff', 3),
                            hw.baseline.get('hstrt', 5), hw.baseline.get('hend', 0),
                            hw.baseline.get('tpfd'))
    if tmc.validate(start) is not None:
        start = tmc.Chopper(2, 3, 5, 0, hw.baseline.get('tpfd'))

    best = coordinate_descent(hw.driver, args.tbl, args.toff, args.hstrt, args.hend, tpfd,
                              start, evaluate)
    finalists = sorted((c for c in cache if cache[c] != float('inf')), key=cache.get)[:VALIDATE_TOP]
    print('Descent best %s; validating top %d with extra runs' % (best.label(), len(finalists)))
    for combo in finalists:
        measure_candidate(combo, 1, first_iteration=args.iterations)

    ranked = rank(aggregate(ds, False, args.trim), hw.driver, args.audible_weight)
    print()
    print_table(ranked, 10)
    print('\nRecommended for printer.cfg:\n')
    print(tmc.cfg_snippet(hw.driver, hw.stepper, ranked[0]['chopper']))
    return stats['ok'], stats['failed']


def run_collect(args) -> int:
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        return collect(kl, args)
    finally:
        kl.close()


def collect(kl: Klippy, args) -> int:
    args.source = 'csv' if args.csv else 'stream'
    if args.trim is None:
        args.trim = 0.25 if args.csv else 0.1

    hw = detect_hardware(kl, args.axis)
    print('Driver tmc%s on %s, accelerometer %s, kinematics %s, baseline %s'
          % (hw.driver.name, hw.stepper, hw.accel_chip, hw.kinematics, hw.baseline))

    tpfd = args.tpfd
    if tpfd is not None and not hw.driver.has_tpfd:
        print('Warning: tmc%s has no TPFD, ignoring --tpfd' % hw.driver.name)
        tpfd = None
    if args.seed_from and args.search != 'descent':
        print('Warning: --seed-from only affects --search descent, ignoring')

    speeds = list(args.speed.values())
    accel = args.accel or hw.max_accel / 10
    travel = max(travel_for(s, accel, args.measure_time) for s in speeds)
    limit = hw.axis_span * MOVE_MARGIN
    if travel > limit:
        raise SystemExit('travel %.0fmm exceeds safe %.0fmm (%.0f%% of %.0fmm axis span); '
                         'reduce --measure-time or raise --accel'
                         % (travel, limit, MOVE_MARGIN * 100, hw.axis_span))

    overhead = OVERHEAD_CSV_SEC if args.csv else OVERHEAD_STREAM_SEC
    per_move = args.measure_time + 2 * max(speeds) / accel + overhead
    plan = []
    if args.search == 'grid':
        plan = build_plan(hw.driver, args.tbl, args.toff, args.hstrt, args.hend, tpfd, speeds,
                          args.skip_audible)
        if not plan:
            raise SystemExit('empty plan: all combinations rejected by datasheet constraints'
                             + (' or audible' if args.skip_audible else ''))
        n_moves = len(plan) * args.iterations * 2
        eta = n_moves * per_move
        print('Plan: %d combinations x %d speeds -> %d moves of %.1fmm, capture %s, ETA %dh %02dm'
              % (len(plan) // len(speeds), len(speeds), n_moves, travel, args.source,
                 eta // 3600, eta % 3600 // 60))
    else:
        from .search import descent_budget
        budget = descent_budget(hw.driver, args.tbl, args.toff, args.hstrt, args.hend, tpfd)
        n_moves = (budget + VALIDATE_TOP) * len(speeds) * args.iterations * 2
        eta = n_moves * per_move
        print('Plan: coordinate descent, up to %d candidates -> up to %d moves of %.1fmm, '
              'capture %s, ETA under %dh %02dm'
              % (budget, n_moves, travel, args.source, eta // 3600, eta % 3600 // 60))
    if args.dry_run:
        return 0
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1

    if not args.csv:
        kl.subscribe_accel(hw.accel_chip)

    root = Path(args.dataset) if args.dataset else default_dataset_root(
        '%s_%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'), args.axis))
    ds = Dataset.create(root, {
        'version': __version__,
        'created': now(),
        'klippy_socket': kl.path,
        'klipper_version': kl.info().get('software_version'),
        'capture': args.source,
        'axis': args.axis,
        'stepper': hw.stepper,
        'driver': hw.driver.name,
        'fclk_hz': hw.driver.fclk_hz,
        'accel_chip': hw.accel_chip,
        'kinematics': hw.kinematics,
        'baseline_registers': hw.baseline,
        'ranges': {'tbl': [args.tbl.lo, args.tbl.hi], 'toff': [args.toff.lo, args.toff.hi],
                   'hstrt': [args.hstrt.lo, args.hstrt.hi], 'hend': [args.hend.lo, args.hend.hi],
                   'tpfd': [tpfd.lo, tpfd.hi] if tpfd else None},
        'search': args.search,
        'audible_weight': args.audible_weight,
        'accel': accel,
        'measure_time': args.measure_time,
        'trim': args.trim,
        'iterations': args.iterations,
        'travel_distance': round(travel, 3),
        'speeds': speeds,
    })
    done = ds.done_ids()
    if done:
        print('Resuming %s: %d measurements already present' % (root, len(done)))

    print('Preparing: home XY, park at center, disable motors')
    park(kl, hw)
    started = time.time()
    before_move = make_parker(kl, hw)
    try:
        measure_baseline(hw, ds, args, done)
        if args.search == 'descent':
            ok, failed = run_descent(kl, hw, ds, args, tpfd, speeds, travel, accel, done, before_move)
        else:
            ok, failed = run_grid(kl, hw, ds, args, plan, travel, accel, done, before_move)
    finally:
        print('Restoring baseline registers, homing')
        if hw.baseline:
            kl.gcode(tmc.set_fields_script(hw.stepper, hw.baseline))
        kl.gcode('G28 X Y')

    print('Done in %dm: %d ok, %d failed -> %s' % ((time.time() - started) // 60, ok, failed, root))
    print('Next: chopper-autotune analyze %s' % root)
    return 0 if failed == 0 else 2
