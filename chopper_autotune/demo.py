"""Demo: measure the driver defaults against the tuned registers, side by side.

Alternates the two configs move by move at the resonance speed so thermal and
timing drift hit both equally, then reports how much quieter the tuned one is.
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
                         '(tune and save the axis first)')

    if args.speed is not None:
        speed = args.speed.lo
    else:
        from .find_speed import scan
        code, speed = scan(kl, _scan_args(args))
        if speed is None:
            raise SystemExit('no clear resonance speed found; pass SPEED=')

    accel = args.accel or hw.max_accel / 10
    cruise = args.measure_time
    travel = travel_for(speed, accel, cruise)
    if travel > hw.axis_span * MOVE_MARGIN:
        raise SystemExit('travel too long for the axis; lower MEASURE_TIME or raise ACCEL')

    print('Demo on %s at %d mm/s: defaults %s vs tuned %s'
          % (hw.stepper, speed, default.label(), tuned.label()))
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
    print('\n%s at %d mm/s (mean vibration, lower is better):\n' % (hw.stepper, speed))
    print('  defaults %-10s %6.0f  %s' % (default.label(), d, bar(d, scale)))
    print('  tuned    %-10s %6.0f  %s' % (tuned.label(), t, bar(t, scale)))
    print('\n  %.2fx quieter overall' % (d / t))
    if noise:
        print('  %.2fx quieter above the %.0f noise floor' % ((d - noise) / (t - noise), noise))
    screen.update('Chopper demo: %.1fx quieter' % (d / t), force=True)
    return 0


def _scan_args(args):
    from argparse import Namespace
    return Namespace(axis=args.axis, csv=False, min_speed=20, max_speed=120, step=2,
                     iterations=1, measure_time=1.0, accel=args.accel, trim=None,
                     dataset=None, no_raw=True, dry_run=False, yes=True)
