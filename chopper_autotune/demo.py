"""Demo: measure the driver defaults against the tuned registers, side by side.

Alternates the two configs move by move at the resonance speed so thermal and
timing drift hit both equally, then reports how much less the tuned one vibrates
(measured at the toolhead, not perceived loudness — see the README caveat).
"""
from __future__ import annotations

import math
import statistics
from datetime import datetime

from . import __version__, tmc
from .collect import (MOVE_MARGIN, Screen, capture_stream, coupled_xy, default_dataset_root,
                      detect_hardware, enter_spreadcycle, exit_spreadcycle, make_parker,
                      measure_baseline, motor_label, now, park, refuse_if_printing,
                      run_measurement, run_restore, travel_for)
from .dataset import Dataset
from .klippy import Klippy, KlippyError, find_socket
from .metrics import vibration_score

KLIPPER_DEFAULT = tmc.KLIPPER_DEFAULT
BAR_WIDTH = 40


def bar(value: float, scale: float) -> str:
    return '#' * max(1, round(BAR_WIDTH * value / scale))


def run_demo(args) -> int:
    from argparse import Namespace
    if getattr(args, 'live', False) and args.report:
        raise SystemExit('LIVE=1 and REPORT=1 contradict — REPORT measures the numbers, '
                         'LIVE plays the audible showcase (the default)')
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        if args.axis == 'xy' and not args.report:
            return showcase_together(kl, args)          # both motors together, like printing
        axes = ['x', 'y'] if args.axis == 'xy' else [args.axis]
        worst = 0
        for axis in axes:
            per_motor = Namespace(**vars(args))
            per_motor.axis = axis
            try:
                worst = max(worst, demo(kl, per_motor))
            except SystemExit as skip:
                # only a per-motor refusal (a string message) is skippable; an integer
                # code is the SIGTERM handler — CHOPPER_STOP must stop the whole demo
                if len(axes) == 1 or not isinstance(skip.code, str):
                    raise
                print('motor %s skipped: %s' % (motor_label(axis), skip))
                worst = max(worst, 2)
        return worst
    finally:
        kl.close()


MOTORS = ('x', 'y')


def showcase_together(kl, args) -> int:
    """Live show for the whole printer: put default vs tuned on BOTH motors and do the same
    coordinated back-and-forth that runs each motor at its own resonance speed at once (a
    diagonal on CoreXY, so the head moves in X and Y and the gantry moves too, like printing),
    so a listener hears the whole machine change, not one motor at a time. Reports the combined
    drop in vibration."""
    hw = {axis: detect_hardware(kl, axis) for axis in MOTORS}
    tpfd = {axis: hw[axis].baseline.get('tpfd') for axis in MOTORS}
    tuned = {axis: tmc.baseline_chopper(hw[axis].baseline, tpfd[axis]) for axis in MOTORS}
    default = {axis: (args.default if args.default is not None
                      else tmc.Chopper(*KLIPPER_DEFAULT.fields().values(), tpfd[axis]))
               for axis in MOTORS}
    if all(tuned[axis] == default[axis] for axis in MOTORS):
        raise SystemExit('current registers equal the defaults on both motors — tune and save first')

    board = hw['x']
    accel = args.accel or board.max_accel / 10
    span = min(hw['x'].axis_span, hw['y'].axis_span)
    speed = {}
    for axis in MOTORS:
        speed[axis] = args.speed.lo if args.speed is not None else known_speed(axis)
        if speed[axis] is None:
            # a silent fallback speed would play the show off resonance and prove nothing
            raise SystemExit('no tuned speed recorded for motor %s — run CHOPPER_TUNE '
                             'or pass SPEED=' % motor_label(axis))
    print('Show on both motors together — diagonal move running motor A at %d and motor B at %d '
          'mm/s at once (both at resonance; head moves in X and Y): defaults vs tuned %s / %s'
          % (speed['x'], speed['y'], tuned['x'].label(), tuned['y'].label()))
    if args.dry_run:
        return 0

    refuse_if_printing(kl)
    kl.subscribe_accel(board.accel_chip)
    screen = Screen(kl, board.display)
    configs = [('default', default), ('tuned', tuned)]
    playing = {'default': '>> DEFAULTS', 'tuned': '>> TUNED'}
    results = {name: [] for name, _ in configs}
    try:
        # home and hold at center with the motors ENABLED (park disables them) for G1 moves
        kl.gcode('G28 X Y\nG90\nM204 S%.0f\nG1 X%.1f Y%.1f F6000\nM400' % (accel, *board.center))
        for axis in MOTORS:
            enter_spreadcycle(kl, hw[axis])
        for r in range(1, args.rounds + 1):
            round_avg = {}
            for name, regs in configs:
                for axis in MOTORS:
                    kl.gcode(tmc.set_fields_script(hw[axis].stepper, regs[axis].fields()))
                screen.update('%d/%d  %s' % (r, args.rounds, playing[name]), force=True)
                print('\n>> round %d/%d  %s — listen (both motors)' % (r, args.rounds, playing[name]))
                mags = _sweep(board, speed, accel, span, args)
                if mags:
                    results[name].extend(mags)
                    round_avg[name] = statistics.mean(mags)
            if 'default' in round_avg and 'tuned' in round_avg:
                factor = round_avg['default'] / round_avg['tuned']
                summary = 'both motors: %.1fx less vibration' % factor
                screen.update(summary, force=True)
                print('   => %s' % summary)
    finally:
        run_restore(
            *[lambda axis=axis: kl.gcode(tmc.set_fields_script(hw[axis].stepper,
                                                               tuned[axis].fields()))
              for axis in MOTORS],
            *[lambda axis=axis: exit_spreadcycle(kl, hw[axis]) for axis in MOTORS],
            lambda: kl.gcode('M204 S%.0f\nG28 X Y' % board.max_accel))

    if not results['default'] or not results['tuned']:
        raise SystemExit('show failed to collect measurements')
    d, t = statistics.mean(results['default']), statistics.mean(results['tuned'])
    print('\nboth motors together: %.2fx less vibration overall' % (d / t))
    screen.final('Chopper: both motors %.1fx less vibration' % (d / t))
    for axis in MOTORS:
        write_state(axis, tuned[axis], d / t)   # combined factor, shown per motor by the panel
    return 0


def head_velocity(kinematics, motor_a, motor_b):
    """Head (X, Y) velocity that runs stepper_x at motor_a and stepper_y at motor_b at once.
    On CoreXY/H-Bot stepper_x = X+Y and stepper_y = X-Y, so a diagonal is needed to give the
    two motors different speeds. On CoreXZ the coupled pair is X/Z, which an X/Y move leaves
    alone (stepper_x follows X, stepper_y is plain Y) — identity, same as Cartesian."""
    if coupled_xy(kinematics):
        return (motor_a + motor_b) / 2.0, (motor_a - motor_b) / 2.0
    return float(motor_a), float(motor_b)


def _sweep(board, speed, accel, span, args):
    """One pass per config: a back-and-forth that runs each motor at its own resonance speed at
    the same time. On CoreXY that means a diagonal (stepper_x at speed['x'], stepper_y at
    speed['y']), so the head moves in both X and Y — the gantry moves too, like a print move.
    Identical stroke and speed for defaults and tuned, so it's a clean before/after."""
    vx, vy = head_velocity(board.kinematics, speed['x'], speed['y'])
    feed = math.hypot(vx, vy)
    cx, cy = board.center
    # sweep the whole allowed zone (not just a measure_time cruise): a longer diagonal runs each
    # motor through more electrical cycles per pass, so the before/after is longer and clearer
    stroke = span * MOVE_MARGIN
    dx, dy = stroke * vx / feed, stroke * vy / feed
    board.kl.gcode('G1 X%.2f Y%.2f F%.0f\nM400' % (cx - dx / 2, cy - dy / 2, feed * 60))
    mags = []
    for _ in range(args.repeats):
        for hx, hy in ((cx + dx / 2, cy + dy / 2), (cx - dx / 2, cy - dy / 2)):
            move = 'G1 X%.2f Y%.2f F%.0f' % (hx, hy, feed * 60)
            try:
                _, data = capture_stream(board, move, stroke / feed + feed / accel)
                mags.append(vibration_score(data, 0.25)['median_magnitude'])
            except (KlippyError, ValueError, TimeoutError, OSError) as failure:
                print('  sweep failed: %s' % failure)
    return mags


def demo(kl: Klippy, args) -> int:
    args.source = 'stream'
    args.csv = False
    args.no_raw = True
    if args.trim is None:
        args.trim = 0.1

    hw = detect_hardware(kl, args.axis)
    tpfd = hw.baseline.get('tpfd')
    tuned = tmc.baseline_chopper(hw.baseline, tpfd)
    default = (args.default if args.default is not None
               else tmc.Chopper(*KLIPPER_DEFAULT.fields().values(), tpfd))
    if tuned == default:
        raise SystemExit('current registers equal the defaults — nothing to demo '
                         '(tune and save the motor first)')

    if args.speed is not None:
        speed = args.speed.lo
    else:
        speed = known_speed(args.axis)
        if speed is not None:
            print('Using resonance speed %d mm/s from the last motor %s run (pass SPEED= to override)'
                  % (speed, hw.motor))
        elif args.dry_run:
            print('Demo on motor %s (dry run): no recorded speed, a resonance scan would run first'
                  % hw.motor)
            return 0
        else:
            print('No previous run to reuse a speed from; scanning for resonance first')
            from .find_speed import scan
            _, speed = scan(kl, _scan_args(args))
            if speed is None:
                raise SystemExit('no clear resonance speed found; pass SPEED=')

    accel = args.accel or hw.max_accel / 10
    cruise = args.measure_time
    travel = travel_for(speed, accel, cruise)
    if travel > hw.axis_span * MOVE_MARGIN:
        raise SystemExit('travel too long for the motor; lower MEASURE_TIME or raise ACCEL')

    live = not args.report
    mode = 'live showcase, %d rounds' % args.rounds if live else 'measure %d each' % args.iterations
    print('Demo on motor %s at %d mm/s (%s): defaults %s vs tuned %s'
          % (hw.motor, speed, mode, default.label(), tuned.label()))
    if args.dry_run:
        return 0

    refuse_if_printing(kl)
    kl.subscribe_accel(hw.accel_chip)
    root = default_dataset_root('%s_demo_%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'), args.axis))
    ds = Dataset.create(root, {'version': __version__, 'created': now(), 'mode': 'demo',
                               'axis': args.axis, 'stepper': hw.stepper, 'driver': hw.driver.name,
                               'speed': speed, 'default': default.label(), 'tuned': tuned.label()})
    print('Preparing: home XY, park at center, disable motors')
    park(kl, hw)
    before_move = make_parker(kl, hw)
    screen = Screen(kl, hw.display)
    configs = [('default', default), ('tuned', tuned)]
    results = {name: [] for name, _ in configs}
    try:
        enter_spreadcycle(kl, hw)
        measure_baseline(hw, ds, args, set())
        if live:
            results = _showcase(kl, hw, args, ds, configs, speed, travel, accel,
                                before_move, screen)
        else:
            for iteration in range(args.iterations):
                for name, combo in configs:
                    kl.gcode(tmc.set_fields_script(hw.stepper, combo.fields()))
                    for direction in (1, -1):
                        record = run_measurement(hw, ds, args, combo, speed, iteration, direction,
                                                 travel, accel, before_move)
                        if record['status'] == 'ok':
                            results[name].append(record['score']['median_magnitude'])
                    screen.update('Chopper demo %s %d/%d' % (name, iteration + 1, args.iterations))
    finally:
        run_restore(
            lambda: kl.gcode(tmc.set_fields_script(hw.stepper, tuned.fields())),
            lambda: exit_spreadcycle(kl, hw),
            lambda: kl.gcode('G28 X Y'))

    if not results['default'] or not results['tuned']:
        raise SystemExit('demo failed to collect measurements')
    d, t = statistics.mean(results['default']), statistics.mean(results['tuned'])
    records = ds.records()
    noise = records[0]['score']['median_magnitude'] if records else 0.0
    scale = max(d, t)
    print('\nmotor %s at %d mm/s (mean vibration, lower is better):\n' % (hw.motor, speed))
    print('  defaults %-10s %6.0f  %s' % (default.label(), d, bar(d, scale)))
    print('  tuned    %-10s %6.0f  %s' % (tuned.label(), t, bar(t, scale)))
    print('\n  %.2fx less vibration overall' % (d / t))
    if noise and t > noise and d > noise:
        print('  %.2fx less vibration above the %.0f noise floor' % ((d - noise) / (t - noise), noise))
    screen.final('Chopper demo: %.1fx less vibration' % (d / t))
    write_state(args.axis, tuned, d / t)
    return 0


def write_state(axis: str, tuned: tmc.Chopper, quieter: float):
    """Record the last measured improvement so the KlipperScreen panel can show how much
    quieter tuning made the motor: RESULTS_HOME/state.json -> {"x": {"regs": "2/1/4/14",
    "quieter": 2.38}, ...}. Keyed by the tuned registers so the panel can drop it as stale
    once the motor is retuned."""
    import json

    from .dataset import RESULTS_HOME
    path = RESULTS_HOME / 'state.json'
    state = {}
    if path.exists():
        try:
            state = json.loads(path.read_text())
        except (ValueError, OSError):
            state = {}
    state[axis] = {'regs': '%d/%d/%d/%d' % (tuned.tbl, tuned.toff, tuned.hstrt, tuned.hend),
                   'quieter': round(quieter, 2)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def _showcase(kl, hw, args, ds, configs, speed, travel, accel, before_move, screen):
    """Play defaults and tuned alternately, announcing on the display/console which is
    playing and the running difference, so a listener hears and sees it change."""
    playing = {'default': '>> DEFAULTS', 'tuned': '>> TUNED'}
    results = {name: [] for name, _ in configs}
    it = 0
    for r in range(1, args.rounds + 1):
        round_avg = {}
        for name, combo in configs:
            kl.gcode(tmc.set_fields_script(hw.stepper, combo.fields()))
            screen.update('%d/%d  %s' % (r, args.rounds, playing[name]), force=True)
            print('\n>> round %d/%d  %s  (%s) — listen' % (r, args.rounds, playing[name],
                                                           combo.label()))
            mags = []
            for _ in range(args.repeats):
                for direction in (1, -1):
                    record = run_measurement(hw, ds, args, combo, speed, it, direction,
                                             travel, accel, before_move)
                    it += 1
                    if record['status'] == 'ok':
                        mags.append(record['score']['median_magnitude'])
            if mags:
                results[name].extend(mags)
                round_avg[name] = statistics.mean(mags)
        if 'default' in round_avg and 'tuned' in round_avg:
            factor = round_avg['default'] / round_avg['tuned']
            summary = 'def %.0f -> tuned %.0f  %.1fx less vibration' % (
                round_avg['default'], round_avg['tuned'], factor)
            screen.update(summary, force=True)
            print('   => %s' % summary)
    return results


def known_speed(axis: str) -> 'int | None':
    """Resonance speed the axis was tuned at, from the most recent collect/descent run,
    so the show starts immediately instead of re-scanning for ~2.5 minutes. Only tuning
    datasets (a 'search' mode, single speed) are trusted — find-speed carries the whole
    scan range and demo datasets only echo whatever speed they were told."""
    from .analyze import dataset_dirs
    for path in reversed(dataset_dirs()):
        manifest = Dataset(path).manifest()
        if manifest.get('axis') != axis or 'search' not in manifest:
            continue
        speeds = manifest.get('speeds') or []
        if len(speeds) == 1:
            return int(speeds[0])
    return None


def _scan_args(args):
    """Through the real find-speed parser (as tune does), so scan defaults live in one place;
    demo() has already set args.csv/no_raw, and dry_run never reaches here (early return)."""
    from .tune import scan_args
    return scan_args(args, args.axis)
