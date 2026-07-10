"""One-command pipeline: resonance speed scan + register descent per motor, optional save.

The second motor is seeded with the first one's winner automatically; with --save
all winners are written into the Klipper config in one batch with a single restart.
"""
from __future__ import annotations

from . import tmc
from .collect import Range, Screen, collect, motor_label
from .dataset import Dataset
from .find_speed import scan
from .klippy import Klippy, find_socket
from .moonraker import Moonraker

DRY_RUN_SPEED = 60


def compact_label(combo: tmc.Chopper) -> str:
    """0/2/4/7 — the shape the panel's register table uses."""
    return '/'.join(str(getattr(combo, field)) for field in ('tbl', 'toff', 'hstrt', 'hend'))


def sub_args(args, argv: 'list[str]'):
    """Build sub-command args through the real parser, so defaults live in one place."""
    from .cli import build_parser
    if args.accel is not None:
        argv += ['--accel', str(args.accel)]
    for flag, enabled in (('--csv', args.csv), ('--no-raw', args.no_raw),
                          ('--dry-run', args.dry_run)):
        if enabled:
            argv.append(flag)
    return build_parser().parse_args(argv)


def scan_args(args, axis: str):
    return sub_args(args, ['find-speed', '--axis', axis, '--yes'])


def collect_args(args, axis: str, speed: Range, seed_from: 'str | None'):
    argv = ['collect', '--axis', axis, '--speed', '%d:%d' % (speed.lo, speed.hi),
            '--search', 'descent', '--tpfd', '0:15',
            '--audible-weight', str(args.audible_weight),
            '--iterations', str(args.iterations), '--yes']
    if seed_from:
        argv += ['--seed-from', seed_from]
    return sub_args(args, argv)


def winner_of(root: str, audible_weight: float) -> 'tuple[dict, tmc.Chopper]':
    from .analyze import aggregate, rank
    ds = Dataset.open(root)
    manifest = ds.manifest()
    saved = manifest.get('winner')
    if saved:
        # the validated recommendation recorded by the run; a full re-rank could
        # instead surface an unvalidated lucky combo (winner's curse)
        return manifest, tmc.Chopper(saved['tbl'], saved['toff'], saved['hstrt'],
                                     saved['hend'], saved.get('tpfd'))
    ranked = rank(aggregate(ds, False, manifest.get('trim') or 0.1),
                  tmc.DRIVERS[manifest['driver']], audible_weight)
    if not ranked:
        raise SystemExit('no successful measurements in %s' % root)
    return manifest, ranked[0]['chopper']


def run_tune(args) -> int:
    axes = ['x', 'y'] if args.axis == 'xy' else [args.axis]
    kl = Klippy(find_socket(args.socket)).connect()
    screen = Screen(kl, True)
    winners = []
    worst = 0
    try:
        seed_root = None
        for axis in axes:
            print('=== Motor %s ===' % motor_label(axis))
            speed = args.speed
            if speed is None:
                code, recommended = scan(kl, scan_args(args, axis))
                worst = max(worst, code)
                if args.dry_run:
                    print('(descent plan below assumes SPEED=%d until the scan runs)'
                          % DRY_RUN_SPEED)
                    recommended = DRY_RUN_SPEED
                elif recommended is None:
                    raise SystemExit('no clear resonance peak on motor %s; tune it manually '
                                     'via CHOPPER_FIND_SPEED + CHOPPER_COLLECT' % motor_label(axis))
                speed = Range(recommended, recommended)
            code, root = collect(kl, collect_args(args, axis, speed, seed_root))
            worst = max(worst, code)
            if root:
                winners.append(winner_of(root, args.audible_weight))
                seed_root = root

        # the run's outcome must reach the screen: the console summary below only lands
        # in the detached log, and SAVE=1 restarts Klipper, wiping the status line — so
        # say how it ended (popup included) while the connection is still alive
        if not args.dry_run and winners:
            labels = ' · '.join(
                '%s %s' % (motor_label(manifest['stepper'].rsplit('_', 1)[-1]),
                           compact_label(combo))
                for manifest, combo in winners)
            screen.final('Tune done: %s%s' % (labels, ' — saving' if args.save
                                              else ' — tap Save to persist'))
    except SystemExit as failure:
        screen.final('Tune FAILED: %s' % failure)      # the display must say why
        raise
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
