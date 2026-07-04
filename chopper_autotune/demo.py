"""Demo: measure the driver defaults against the tuned registers, side by side.

Alternates the two configs move by move at the resonance speed so thermal and
timing drift hit both equally, then reports how much less the tuned one vibrates
(measured at the toolhead, not perceived loudness — see the README caveat).
"""
from __future__ import annotations

import statistics
from datetime import datetime

from . import __version__, tmc
from .collect import (MOVE_MARGIN, Screen, default_dataset_root, detect_hardware,
                      enter_spreadcycle, exit_spreadcycle, make_parker, measure_baseline,
                      now, park, run_measurement, travel_for)
from .dataset import Dataset
from .klippy import Klippy, find_socket

KLIPPER_DEFAULT = tmc.Chopper(2, 3, 5, 0)
BAR_WIDTH = 40


def bar(value: float, scale: float) -> str:
    return '#' * max(1, round(BAR_WIDTH * value / scale))


def run_demo(args) -> int:
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        return demo(kl, args)
    finally:
        kl.close()


def demo(kl: Klippy, args) -> int:
    args.source = 'stream'
    args.csv = False
    args.no_raw = True
    if args.trim is None:
        args.trim = 0.1

    hw = detect_hardware(kl, args.axis)
    tpfd = hw.baseline.get('tpfd')
    tuned = tmc.Chopper(hw.baseline.get('tbl', 2), hw.baseline.get('toff', 3),
                        hw.baseline.get('hstrt', 5), hw.baseline.get('hend', 0), tpfd)
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

    kl.subscribe_accel(hw.accel_chip)
    root = default_dataset_root('%s_demo_%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'), args.axis))
    ds = Dataset.create(root, {'version': __version__, 'created': now(), 'mode': 'demo',
                               'axis': args.axis, 'stepper': hw.stepper, 'driver': hw.driver.name,
                               'speed': speed, 'default': default.label(), 'tuned': tuned.label()})
    print('Preparing: home XY, park at center, disable motors')
    park(kl, hw)
    enter_spreadcycle(kl, hw)
    before_move = make_parker(kl, hw)
    screen = Screen(kl, hw.display)
    configs = [('default', default), ('tuned', tuned)]
    results = {name: [] for name, _ in configs}
    try:
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
        kl.gcode(tmc.set_fields_script(hw.stepper, tuned.fields()))
        exit_spreadcycle(kl, hw)
        kl.gcode('G28 X Y')

    if not results['default'] or not results['tuned']:
        raise SystemExit('demo failed to collect measurements')
    d, t = statistics.mean(results['default']), statistics.mean(results['tuned'])
    noise = ds.records()[0]['score']['median_magnitude'] if ds.records() else 0.0
    scale = max(d, t)
    print('\nmotor %s at %d mm/s (mean vibration, lower is better):\n' % (hw.motor, speed))
    print('  defaults %-10s %6.0f  %s' % (default.label(), d, bar(d, scale)))
    print('  tuned    %-10s %6.0f  %s' % (tuned.label(), t, bar(t, scale)))
    print('\n  %.2fx less vibration overall' % (d / t))
    if noise:
        print('  %.2fx less vibration above the %.0f noise floor' % ((d - noise) / (t - noise), noise))
    screen.update('Chopper demo: %.1fx less vibration' % (d / t), force=True)
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
    from argparse import Namespace
    return Namespace(axis=args.axis, csv=False, min_speed=20, max_speed=120, step=2,
                     iterations=1, measure_time=1.0, accel=args.accel, trim=None,
                     dataset=None, no_raw=True, dry_run=False, yes=True)
