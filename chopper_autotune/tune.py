"""One-command pipeline: resonance speed scan + register descent per axis, optional save.

The second axis is seeded with the first one's winner automatically; with --save
all winners are written into the Klipper config in one batch with a single restart.
"""
from __future__ import annotations

from argparse import Namespace

from . import tmc
from .collect import Range, collect
from .dataset import Dataset
from .find_speed import scan
from .klippy import Klippy, find_socket
from .moonraker import Moonraker

DRY_RUN_SPEED = 60


def scan_args(args, axis: str) -> Namespace:
    return Namespace(axis=axis, csv=args.csv, min_speed=20, max_speed=120, step=2,
                     iterations=1, measure_time=1.0, accel=args.accel, trim=None,
                     dataset=None, no_raw=args.no_raw, dry_run=args.dry_run, yes=True)


def collect_args(args, axis: str, speed: Range, seed_from: 'str | None') -> Namespace:
    return Namespace(axis=axis, csv=args.csv, speed=speed, tbl=Range(0, 3), toff=Range(1, 8),
                     hstrt=Range(0, 7), hend=Range(0, 15), tpfd=Range(0, 15),
                     search='descent', audible_weight=args.audible_weight,
                     seed_from=seed_from, iterations=args.iterations, validate=3, skip_audible=False,
                     measure_time=1.25, accel=args.accel, trim=None, dataset=None,
                     no_raw=args.no_raw, dry_run=args.dry_run, yes=True)


def winner_of(root: str, audible_weight: float) -> 'tuple[dict, tmc.Chopper]':
    from .analyze import aggregate, rank
    ds = Dataset.open(root)
    manifest = ds.manifest()
    ranked = rank(aggregate(ds, False, manifest.get('trim') or 0.1),
                  tmc.DRIVERS[manifest['driver']], audible_weight)
    return manifest, ranked[0]['chopper']


def run_tune(args) -> int:
    axes = ['x', 'y'] if args.axis == 'xy' else [args.axis]
    kl = Klippy(find_socket(args.socket)).connect()
    winners = []
    worst = 0
    try:
        seed_root = None
        for axis in axes:
            print('=== Axis %s ===' % axis.upper())
            speed = args.speed
            if speed is None:
                code, recommended = scan(kl, scan_args(args, axis))
                worst = max(worst, code)
                if args.dry_run:
                    print('(descent plan below assumes SPEED=%d until the scan runs)'
                          % DRY_RUN_SPEED)
                    recommended = DRY_RUN_SPEED
                elif recommended is None:
                    raise SystemExit('no clear resonance peak on %s; tune this axis manually '
                                     'via CHOPPER_FIND_SPEED + CHOPPER_COLLECT' % axis.upper())
                speed = Range(recommended, recommended)
            code, root = collect(kl, collect_args(args, axis, speed, seed_root))
            worst = max(worst, code)
            if root:
                winners.append(winner_of(root, args.audible_weight))
                seed_root = root
    finally:
        kl.close()

    if args.dry_run or not winners:
        return worst

    print('\n=== Summary ===')
    for manifest, combo in winners:
        print(tmc.cfg_snippet(tmc.DRIVERS[manifest['driver']], manifest['stepper'], combo))
        print()
    if args.save:
        from .analyze import run_save
        run_save(Moonraker(args.url), winners)
    else:
        print('Re-run with SAVE=1 to write this into the config, or paste it manually')
    return worst
