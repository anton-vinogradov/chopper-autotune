"""One-command pipeline: resonance speed scan + register descent per motor, optional save.

The second motor is seeded with the first one's winner automatically; with --save
all winners are written into the Klipper config in one batch with a single restart.
"""
from __future__ import annotations

from pathlib import Path

from . import tmc
from .collect import (Range, Screen, autotune_advice, autotune_goal, collect, driver_of,
                      motor_label, refuse_autotune_save, refuse_multi_motor, refuse_unhearable,
                      run_motors, save_command, tell_next, worth_saving)
from .dataset import Dataset
from .find_speed import no_peak, scan, tune_at
from .klippy import Klippy, find_socket

DRY_RUN_SPEED = 60


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
            '--iterations', str(args.iterations), '--yes']
    for flag, value in (('--audible-khz', args.audible_khz), ('--audible-weight', args.audible_weight)):
        if value is not None:
            argv += [flag, repr(value)]
    if args.skip_audible:
        argv.append('--skip-audible')
    if seed_from:
        argv += ['--seed-from', seed_from]
    return sub_args(args, argv)


def winner_of(root: str, args=None) -> 'tuple[dict, tmc.Chopper]':
    """The motor's result in a dataset: the winner its run recorded, unless the hearing
    (the command line's, then the run's) skips it; else a re-rank with that hearing."""
    from .analyze import aggregate, rank
    ds = Dataset.open(root)
    manifest = ds.manifest()
    saved = manifest.get('winner')
    driver = tmc.DRIVERS[manifest['driver']]
    hearing = tmc.Hearing.of(args, manifest)
    if saved:
        # the validated recommendation recorded by the run; a full re-rank could
        # instead surface an unvalidated lucky combo (winner's curse)
        winner = tmc.Chopper(saved['tbl'], saved['toff'], saved['hstrt'], saved['hend'],
                             saved.get('tpfd'))
        if tmc.validate(winner, driver) is None:
            if not hearing.skips(winner, driver):
                return manifest, winner
            print('The winner recorded in %s, %s, runs below %g kHz (SKIP_AUDIBLE): '
                  'taking the best inaudible combo instead'
                  % (ds.root.name, winner.label(), hearing.limit_hz / 1000))
    aggregates = aggregate(ds, False, manifest.get('trim') or 0.1)
    ranked = rank(aggregates, driver, hearing)
    if not ranked and aggregates and hearing.skip:
        # the action first: the display shows its first characters (failure_display)
        raise SystemExit('re-run CHOPPER_TUNE with AUDIBLE_KHZ=%g: SKIP_AUDIBLE skips all of %s'
                         % (hearing.limit_hz / 1000, ds.root.name))
    if not ranked:
        raise SystemExit('no successful measurements in %s' % root)
    return manifest, ranked[0]['chopper']


def improvement_note(manifest: dict) -> str:
    """' -46%' when the run measured the winner quieter than Klipper defaults; ''
    when the reference was not measured or did not lose."""
    quieter = manifest.get('improvement') or 0
    pct = round((1 - 1 / quieter) * 100) if quieter > 1 else 0
    return ' -%d%%' % pct if pct >= 1 else ''


def left_undone(args, settings: dict, axes: 'list[str]', axis: str,
                winners: 'list[tuple[dict, tmc.Chopper]]') -> 'list[str]':
    """What a run that stops at the motor on `axis` leaves to do: save the motors it tuned
    before, tune the ones after."""
    from .cli import command_line
    lines = []
    tuned = worth_saving(settings, [manifest for manifest, _ in winners])
    if tuned:
        done = run_motors(tuned)
        lines.append('The run tuned motor %s. To save it: %s' % (done, save_command(tuned)))
    rest = ''.join(axes[axes.index(axis) + 1:])
    if rest:
        later = motor_label(rest)
        lines.append('The run did not reach motor %s. To tune it: %s'
                     % (later, command_line('tune', args, axis=rest)))
    return lines


def run_tune(args) -> int:
    axes = ['x', 'y'] if args.axis == 'xy' else [args.axis]
    kl = Klippy(find_socket(args.socket)).connect()
    settings = kl.settings()

    def managed(manifest: dict) -> bool:
        """klipper_tmc_autotune manages the motor now (a TMC2208 included, whose result
        carries no 'measured under autotune' tag): pasting or saving would be undone."""
        return autotune_goal(settings, manifest['stepper']) is not None

    refuse_multi_motor(settings, args.axis)
    for axis in axes:
        # say it now, not after the speed scan
        name = driver_of(settings, 'stepper_' + axis)
        if name:
            search = collect_args(args, axis, Range(1, 1), None)
            refuse_unhearable(tmc.DRIVERS[name], search.tbl, search.toff, tmc.Hearing.of(search))
    if args.save:
        # say it now, not after twenty minutes of tuning
        for axis in axes:
            stepper = 'stepper_' + axis
            refuse_autotune_save(settings, driver_of(settings, stepper) or 'XXXX', stepper)
    screen = Screen(kl, True)
    winners, roots = [], []
    worst = 0
    try:
        seed_root = None
        for axis in axes:
            print('=== Motor %s ===' % motor_label(axis))
            speed = args.speed
            if speed is None:
                code, recommended, loudest = scan(kl, scan_args(args, axis), popup=False)
                worst = max(worst, code)
                if args.dry_run:
                    print('(descent plan below assumes SPEED=%d until the scan runs)'
                          % DRY_RUN_SPEED)
                    recommended = DRY_RUN_SPEED
                elif recommended is None:
                    tell_next(kl, left_undone(args, settings, axes, axis, winners))
                    raise SystemExit(no_peak(motor_label(axis), tune_at(axis, loudest, args)))
                speed = Range(recommended, recommended)
            code, root = collect(kl, collect_args(args, axis, speed, seed_root), popup=False)
            worst = max(worst, code)
            if root:
                winners.append(winner_of(root, args))
                roots.append(Path(root))
                seed_root = root
        runs = [manifest for manifest, _ in winners]
        free = [manifest for manifest in runs if not managed(manifest)]
        changed = worth_saving(settings, runs)

        # the run's outcome must reach the screen: the console summary below only lands
        # in the detached log, and SAVE=1 restarts Klipper, wiping the status line — so
        # say how it ended (popup included) while the connection is still alive
        if not args.dry_run and winners:
            # a motor klipper_tmc_autotune manages now is marked: Save skips it
            labels = ' · '.join(
                '%s %s%s%s%s' % (motor_label(manifest['stepper'].rsplit('_', 1)[-1]),
                                 'keep ' if manifest.get('kept') else '', combo.compact(),
                                 improvement_note(manifest),
                                 ' (autotune)' if managed(manifest) else '')
                for manifest, combo in winners)
            tail = (' — autotune resets these at start' if not free
                    else ' — your registers stay' if not changed
                    else ' — saving' if args.save
                    else ' — to save: ' + save_command(runs))
            screen.final('Tune done: %s%s' % (labels, tail), 'Done ' + ' '.join(
                motor_label(manifest['stepper'].rsplit('_', 1)[-1]) + improvement_note(manifest).strip()
                for manifest, _ in winners))
    finally:
        kl.close()

    if args.dry_run or not winners:
        return worst

    print('\n=== Summary ===')
    for manifest, combo in winners:
        if managed(manifest):
            # no snippet: pasting it would change nothing, autotune writes its own at start
            print('%s: %s\n' % (manifest['stepper'],
                                autotune_advice(settings, manifest['driver'], manifest['stepper'])))
            continue
        print(tmc.cfg_snippet(tmc.DRIVERS[manifest['driver']], manifest['stepper'], combo))
        if manifest.get('kept'):
            print('# your registers, kept: no combo was quieter in each back-to-back round')
        if manifest.get('improvement'):
            print('# %.1fx less vibration than Klipper defaults\n'
                  % manifest['improvement'])
        print()
    if args.save:
        from .analyze import run_save_latest
        run_save_latest(args, run_motors(runs), roots)
    elif changed:
        print('To save: %s' % save_command(runs))
    return worst
