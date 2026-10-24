"""Demo: measure the driver defaults against the tuned registers, side by side.

Alternates the two configs move by move at the resonance speed so thermal and
timing drift hit both equally, then reports how much less the tuned one vibrates
(measured at the toolhead, not perceived loudness — see the README caveat).
"""
from __future__ import annotations

import math
import statistics
from dataclasses import replace
from datetime import datetime

from . import __version__, tmc
from .collect import (MOVE_MARGIN, Hardware, RunStopped, Screen, ThermalGuard, capture_stream,
                      coupled_xy, default_dataset_root, detect_hardware, enter_spreadcycle,
                      exit_spreadcycle, fit_measure_time, home_xy, make_parker, measure_baseline,
                      motor_label, now, park, refuse_after_shutdown, refuse_blind_z_hop,
                      refuse_if_printing, refuse_multi_motor, rehome_unless_hot, restore_chopper,
                      run_measurement, run_restore, travel_for)
from .current import live_limits
from .dataset import Dataset
from .klippy import Klippy, KlippyError, find_socket
from .metrics import vibration_score

BAR_WIDTH = 40


def bar(value: float, scale: float) -> str:
    return '#' * max(1, round(BAR_WIDTH * value / scale))


def run_demo(args) -> int:
    from argparse import Namespace
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        # before the per-motor loop: a refusal there reads as 'motor A/B skipped', after
        # motor A played, and only in the log
        refuse_multi_motor(kl.settings(), args.axis)
        axes = ['x', 'y'] if args.axis == 'xy' else [args.axis]
        if not args.dry_run:
            refuse_if_printing(kl)              # first: the refusal below says to restart Klipper
            refuse_set_live(kl, [detect_hardware(kl, axis, accel=False) for axis in axes])
        if args.speed is not None and args.axis == 'xy':
            refuse_one_speed(kl, args)
        if args.axis == 'xy' and not args.report:
            return showcase_together(kl, args)          # both motors together, like printing
        worst = 0
        for axis in axes:
            per_motor = Namespace(**vars(args))
            per_motor.axis = axis
            try:
                worst = max(worst, demo(kl, per_motor))
            except SystemExit as skip:
                # only a per-motor refusal (a string message) is skippable; an integer
                # code is the SIGTERM handler — CHOPPER_STOP must stop the whole demo
                if len(axes) == 1 or not isinstance(skip.code, str) or isinstance(skip, RunStopped):
                    raise
                print('motor %s skipped: %s' % (motor_label(axis), skip))
                worst = max(worst, 2)
        return worst
    finally:
        kl.close()


MOTORS = ('x', 'y')


def before_registers(args, driver: tmc.Driver) -> tmc.Chopper:
    """The 'defaults' round: the driver's stock registers, or DEFAULT= checked for this
    driver (TMC2660 caps hstrt + hend), with the stock tpfd where it has one: a Chopper
    without tpfd leaves the tuned value in the chip."""
    if args.default is None:
        return driver.default
    problem = tmc.validate(args.default, driver)
    if problem is not None:
        raise RunStopped('DEFAULT=%s: %s' % (args.default.label(), problem))
    return replace(args.default, tpfd=driver.default.tpfd) if driver.has_tpfd else args.default


def plays_same(hw: Hardware, default: tmc.Chopper) -> bool:
    """Whether the config Klipper loaded gives the motor the registers of its defaults
    round, so both rounds play the same. klipper_tmc_autotune's motor runs its own
    registers, not its config lines: judged once read live."""
    return hw.autotune is None and tmc.baseline_chopper(hw.baseline,
                                                        default=hw.driver.default) == default


def refuse_one_speed(kl: Klippy, args):
    """Refuse SPEED= on both motors, each with its own resonance speed, naming the demo of the
    motor SPEED= is for: one with something to play, without a speed from its last tune
    (known_speed) where there is one. The show of both refuses such a motor, the report
    scans it."""
    from .cli import command_line
    hw = {axis: detect_hardware(kl, axis, accel=False) for axis in MOTORS}
    default = {axis: before_registers(args, hw[axis].driver) for axis in MOTORS}
    playing = [axis for axis in MOTORS if not plays_same(hw[axis], default[axis])]
    if not playing:
        refuse_on_defaults([(hw[axis], default[axis]) for axis in MOTORS])
    axis = next((axis for axis in playing if known_speed(axis) is None), playing[0])
    raise SystemExit('add MOTOR=A or B, or leave SPEED out: each motor has its own resonance '
                     'speed. Without SPEED, each motor plays at the speed of its last tune. '
                     'To play only motor %s at this speed: %s'
                     % (motor_label(axis), command_line('demo', args, axis=axis)))


def on_defaults(hw: Hardware, default: tmc.Chopper) -> str:
    """'the Klipper defaults tbl2_… to stepper_y': what the config Klipper loaded gives the
    driver of a motor whose demo plays the same registers in both rounds."""
    if default == hw.driver.default:
        return 'the Klipper defaults %s to %s' % (default.label(), hw.stepper)
    return 'the DEFAULT= registers %s to %s' % (default.label(), hw.stepper)


def refuse_on_defaults(motors: 'list[tuple[Hardware, tmc.Chopper]]'):
    """The refusal of a demo where the config gives each motor (hw, the registers of its
    defaults round) those very registers, once its driver is known to run them
    (refuse_set_live): tune and save, or, where DEFAULT= is the saved set, pass another."""
    gives = ', and '.join(on_defaults(hw, default) for hw, default in motors)
    # the action first: the display shows its first characters (failure_display)
    if any(default != hw.driver.default for hw, default in motors):
        raise SystemExit('pass another DEFAULT= or drop it, then retry: the config that Klipper '
                         "loaded gives %s. So the demo's defaults and tuned registers would be "
                         'the same. Nothing was moved' % gives)
    raise SystemExit('tune and save motor%s %s first: the config that Klipper loaded gives %s. '
                     "So the demo's defaults and tuned registers would be the same. If you "
                     'edited the config after Klipper started, restart Klipper (RESTART). '
                     'Nothing was moved'
                     % ('s' if len(motors) > 1 else '', ' and '.join(hw.motor for hw, _ in motors),
                        gives))


def refuse_set_live(kl: Klippy, hws: 'list[Hardware]'):
    """Refuse a demo whose drivers run other registers than the config Klipper loaded gives
    them, set live (CHOPPER_ANALYZE APPLY=1, SET_TMC_FIELD). The demo plays the config's
    registers and puts them back at its end, which would undo the live ones. A driver Klipper
    switched off with toff 0 gets the config's toff back when it goes on, so its toff is not
    compared; klipper_tmc_autotune's motors are read live anyway."""
    apart, motors = {}, set()
    for hw in hws:
        if hw.autotune is not None:
            continue
        config = tmc.baseline_chopper(hw.baseline, default=hw.driver.default)
        try:
            lines = kl.gcode_output('DUMP_TMC STEPPER=%s REGISTER=CHOPCONF' % hw.stepper)
        except KlippyError:
            continue
        live = {name: tmc.parse_dump_field(lines, 'CHOPCONF', name) for name in config.fields()}
        if live['toff'] == 0:
            live['toff'] = config.toff
        if None in live.values() or live == config.fields():
            continue
        apart.setdefault((tmc.Chopper(**live).label(), config.label()), []).append(hw.stepper)
        motors.add(hw.motor)
    if not apart:
        return
    runs = '; '.join('%s %s %s, but the config that Klipper loaded gives %s %s'
                     % (' and '.join(steppers), 'run' if len(steppers) > 1 else 'runs', live,
                        'them' if len(steppers) > 1 else 'it', config)
                     for (live, config), steppers in apart.items())
    # without MOTOR= that CHOPPER_ANALYZE takes the newest run of any motor
    analyze = ('repeat for each motor the CHOPPER_ANALYZE that set its registers, with MOTOR=A or '
               'MOTOR=B added' if len(motors) > 1 else
               'repeat the CHOPPER_ANALYZE that set them, with MOTOR=%s added' % motors.pop())
    # the action first: the display shows its first characters (failure_display)
    raise SystemExit('save or drop the registers set live, then retry: %s. The demo plays the '
                     'registers of that config. At its end it puts them back, which would undo '
                     'the ones set live. To keep them, %s and SAVE=1 in place of APPLY=1. Or '
                     'write them into the config and restart Klipper (RESTART). To drop them, '
                     'restart Klipper. Nothing was moved' % (runs, analyze))


def showcase_together(kl, args) -> int:
    """Live show for the whole printer: put default vs tuned on BOTH motors and do the same
    coordinated back-and-forth that runs each motor at its own resonance speed at once (a
    diagonal on CoreXY, so the head moves in X and Y and the gantry moves too, like printing),
    so a listener hears the whole machine change, not one motor at a time. Reports the combined
    drop in vibration."""
    from .cli import command_line
    hw = {axis: detect_hardware(kl, axis) for axis in MOTORS}
    tpfd = {axis: hw[axis].baseline.get('tpfd') for axis in MOTORS}
    tuned = {axis: tmc.baseline_chopper(hw[axis].baseline, tpfd[axis], hw[axis].driver.default)
             for axis in MOTORS}
    default = {axis: before_registers(args, hw[axis].driver) for axis in MOTORS}
    alike = [axis for axis in MOTORS if plays_same(hw[axis], default[axis])]
    if len(alike) == len(MOTORS):
        refuse_on_defaults([(hw[axis], default[axis]) for axis in MOTORS])

    board = hw['x']
    accel = args.accel or board.max_accel / 10
    span = min(hw['x'].axis_span, hw['y'].axis_span)
    speed = {}
    for axis in MOTORS:
        speed[axis] = known_speed(axis)
        if speed[axis] is None:
            # a silent fallback speed would play the show off resonance and prove nothing.
            # Alone, a motor whose registers equal the defaults has nothing to demo (demo)
            if axis in alike:
                raise SystemExit('no tuned speed recorded for motor %s. Its registers equal the '
                                 'defaults, so it has nothing to show. To tune it: %s'
                                 % (motor_label(axis), command_line('tune', axis=axis)))
            raise SystemExit('no tuned speed recorded for motor %s. The command that plays only '
                             'motor %s scans for its resonance speed first: %s'
                             % (motor_label(axis), motor_label(axis),
                                command_line('demo', args, axis=axis)))
    print('Show on both motors together — diagonal move running motor A at %d and motor B at %d '
          'mm/s at once (both at resonance; head moves in X and Y): defaults vs tuned %s / %s'
          % (speed['x'], speed['y'], tuned['x'].label(), tuned['y'].label()))
    same_rounds = []
    for axis in alike:
        motor = hw[axis].motor
        hear = ('Tune and save motor %s to hear it change too' % motor
                if default[axis] == hw[axis].driver.default else
                'Pass another DEFAULT= or drop it to hear motor %s change too' % motor)
        same_rounds.append('Motor %s plays the same registers in DEFAULTS and TUNED. The config '
                           'that Klipper loaded gives %s. %s'
                           % (motor, on_defaults(hw[axis], default[axis]), hear))
        print(same_rounds[-1])
    if args.dry_run:
        return 0

    refuse_if_printing(kl)
    kl.subscribe_accel(board.accel_chip)
    screen = Screen(kl, board.display)
    configs = [('default', default), ('tuned', tuned)]
    playing = {'default': '>> DEFAULTS', 'tuned': '>> TUNED'}
    results = {name: [] for name, _ in configs}
    guard = ThermalGuard(kl, kl.settings())
    # outside the try: after its release the finally would write toff into the switched-off
    # drivers, which re-energizes a hot one on a stepper without an enable pin
    refuse_blind_z_hop(kl, kl.settings())
    guard.preflight()
    accel_now = live_limits(kl)['max_accel']        # an M204 left from a print comes back
    for line in same_rounds:
        screen.tell(line)
    try:
        # home and hold at center with the motors ENABLED (park disables them) for G1 moves
        home_xy(kl, 'G28 X Y\nG90\nM204 S%.0f\nG1 X%.1f Y%.1f F6000\nM400' % (accel, *board.center))
        for axis in MOTORS:
            enter_spreadcycle(kl, hw[axis])
            # on autotune's motor the registers it runs, read live: 'tuned' plays and
            # puts back what the motor runs, not config lines autotune overrides
            tuned[axis] = tmc.baseline_chopper(hw[axis].baseline, hw[axis].baseline.get('tpfd'),
                                               hw[axis].driver.default)
        if all(tuned[axis] == default[axis] for axis in MOTORS):
            raise SystemExit('the drivers run the defaults on both motors — nothing to show')
        for r in range(1, args.rounds + 1):
            round_avg = {}
            for name, regs in configs:
                for axis in MOTORS:
                    kl.gcode(tmc.set_fields_script(hw[axis].stepper, regs[axis].fields()))
                screen.update('%d/%d  %s' % (r, args.rounds, playing[name]), force=True,
                              short='%d/%d %s' % (r, args.rounds, name.upper()))
                print('\n>> round %d/%d  %s — listen (both motors)' % (r, args.rounds, playing[name]))
                mags = _sweep(board, speed, accel, span, args, guard.check)
                if mags:
                    results[name].extend(mags)
                    round_avg[name] = statistics.mean(mags)
            if 'default' in round_avg and 'tuned' in round_avg:
                factor = round_avg['default'] / round_avg['tuned']
                summary = 'both motors: %.1fx less vibration' % factor
                screen.update(summary, force=True, short='%.1fx less vib' % factor)
                print('   => %s' % summary)
    finally:
        run_restore(
            *[lambda axis=axis: restore_chopper(kl, hw[axis]) for axis in MOTORS],
            *[lambda axis=axis: exit_spreadcycle(kl, hw[axis]) for axis in MOTORS],
            lambda: kl.gcode('M204 S%.0f' % accel_now),
            lambda: rehome_unless_hot(kl))

    if not results['default'] or not results['tuned']:
        raise SystemExit('show failed to collect measurements')
    d, t = statistics.mean(results['default']), statistics.mean(results['tuned'])
    # a motor that played one set in both rounds: the panel shows no gain for it
    same = [axis for axis in MOTORS if tuned[axis] == default[axis]]
    note = ''.join('; motor %s played the same registers in DEFAULTS and TUNED'
                   % motor_label(axis) for axis in same)
    print('\nboth motors together: %.2fx less vibration overall%s' % (d / t, note))
    screen.final('Chopper: both motors together %.1fx less vibration%s' % (d / t, note),
                 '%.1fx less vib' % (d / t))
    for axis in MOTORS:
        # combined factor, shown per motor by the panel
        write_state(axis, tuned[axis], None if axis in same else d / t)
    return 0


def head_velocity(kinematics, motor_a, motor_b):
    """Head (X, Y) velocity that runs stepper_x at motor_a and stepper_y at motor_b at once.
    On CoreXY/H-Bot stepper_x = X+Y and stepper_y = X-Y, so a diagonal is needed to give the
    two motors different speeds. On CoreXZ the coupled pair is X/Z, which an X/Y move leaves
    alone (stepper_x follows X, stepper_y is plain Y) — identity, same as Cartesian."""
    if coupled_xy(kinematics):
        return (motor_a + motor_b) / 2.0, (motor_a - motor_b) / 2.0
    return float(motor_a), float(motor_b)


def _sweep(board, speed, accel, span, args, check=lambda: None):
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
    check()
    board.kl.gcode('G1 X%.2f Y%.2f F%.0f\nM400' % (cx - dx / 2, cy - dy / 2, feed * 60))
    mags = []
    for _ in range(args.repeats):
        for hx, hy in ((cx + dx / 2, cy + dy / 2), (cx - dx / 2, cy - dy / 2)):
            check()
            move = 'G1 X%.2f Y%.2f F%.0f' % (hx, hy, feed * 60)
            try:
                _, data = capture_stream(board, move, stroke / feed + feed / accel)
                mags.append(vibration_score(data, 0.25)['median_magnitude'])
            except (KlippyError, ValueError, TimeoutError, OSError) as failure:
                refuse_after_shutdown(failure)
                print('  sweep failed: %s' % failure)
    return mags


def demo(kl: Klippy, args) -> int:
    from .cli import command_line
    args.source = 'stream'
    args.csv = False
    args.no_raw = True

    hw = detect_hardware(kl, args.axis)
    tpfd = hw.baseline.get('tpfd')
    tuned = tmc.baseline_chopper(hw.baseline, tpfd, hw.driver.default)
    default = before_registers(args, hw.driver)
    if plays_same(hw, default):
        refuse_on_defaults([(hw, default)])

    if args.speed is not None:
        speed = args.speed
    else:
        speed = known_speed(args.axis)
        if speed is not None:
            # SPEED= without MOTOR= refuses where the demo plays both motors (run_demo)
            print('Using resonance speed %d mm/s from the last motor %s run (pass SPEED= with '
                  'MOTOR=%s to override)' % (speed, hw.motor, hw.motor))
        elif args.dry_run:
            print('Demo on motor %s (dry run): no recorded speed, a resonance scan would run first'
                  % hw.motor)
            return 0
        else:
            print('No previous run to reuse a speed from; scanning for resonance first')
            from .find_speed import scan
            _, speed, loudest = scan(kl, _scan_args(args))
            if speed is None:
                raise SystemExit('no clear resonance speed found. To play motor %s at the speed it '
                                 'vibrates most: %s'
                                 % (hw.motor, command_line('demo', args, speed=loudest)))
    # after the hints above: a TRIM= the person never gave stays out of the commands they name
    if args.trim is None:
        args.trim = 0.1

    accel = args.accel or hw.max_accel / 10
    # a high resonance speed can push the default cruise past the axis (measured: the
    # re-tensioned rig moved motor B's resonance to ~100 mm/s and the demo refused) —
    # shrink the cruise like collect does instead of skipping the motor
    cruise = fit_measure_time([int(speed)], accel, hw.axis_span * MOVE_MARGIN,
                              args.measure_time)
    if cruise < args.measure_time:
        print('Cruise %.2fs does not fit the axis at %d mm/s: shrinking to %.2fs'
              % (args.measure_time, speed, cruise))
        args.measure_time = cruise               # run_measurement slices by this window
    travel = travel_for(speed, accel, cruise)

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
    print('Preparing: home XY, park at center, switch the gantry and head motors off')
    guard = ThermalGuard(kl, kl.settings())
    refuse_blind_z_hop(kl, kl.settings())
    guard.preflight()
    park(kl, hw)
    before_move = make_parker(kl, hw, guard)
    screen = Screen(kl, hw.display)
    configs = [('default', default), ('tuned', tuned)]
    results = {name: [] for name, _ in configs}
    try:
        measure_baseline(hw, ds, args, set())      # the noise floor: motors still off
        enter_spreadcycle(kl, hw)
        # as in showcase_together: on autotune's motor, what it runs, read live
        tuned = tmc.baseline_chopper(hw.baseline, hw.baseline.get('tpfd'), hw.driver.default)
        if tuned == default:
            raise SystemExit('klipper_tmc_autotune runs the driver defaults on motor %s — nothing '
                             'to demo' % hw.motor)
        configs = [('default', default), ('tuned', tuned)]
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
                    screen.update('Chopper demo %s %d/%d' % (name, iteration + 1, args.iterations),
                                  short='%s %s %d/%d' % (hw.motor, name, iteration + 1,
                                                         args.iterations))
    finally:
        run_restore(
            lambda: restore_chopper(kl, hw),
            lambda: exit_spreadcycle(kl, hw),
            lambda: rehome_unless_hot(kl),
            ds.flush_raw)

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
    screen.final('Chopper demo: %.1fx less vibration' % (d / t), '%s %.1fx less vib' % (hw.motor, d / t))
    write_state(args.axis, tuned, d / t)
    return 0


def write_state(axis: str, tuned: tmc.Chopper, quieter: 'float | None'):
    """Record the last measured improvement so the KlipperScreen panel can show how much
    quieter tuning made the motor: RESULTS_HOME/state.json -> {"x": {"regs": "2/1/4/14",
    "quieter": 2.38}, ...}. Keyed by the tuned registers so the panel can drop it as stale
    once the motor is retuned; None when a run measured no gain, so the panel shows none."""
    from .dataset import RESULTS_HOME, save_json
    save_json(RESULTS_HOME / 'state.json',
              {axis: {'regs': '/'.join(str(v) for v in tuned.fields().values()),
                      'quieter': round(quieter, 2) if quieter else None}}, merge=True)


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
            screen.update('%d/%d  %s' % (r, args.rounds, playing[name]), force=True,
                          short='%d/%d %s' % (r, args.rounds, name.upper()))
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
            screen.update(summary, force=True, short='%s %.1fx less vib' % (hw.motor, factor))
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
