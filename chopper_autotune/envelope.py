"""Motor motion envelope: how fast and how hard each motor can be pushed before it
loses steps, at the configured run current, via the endstop referee.

This is the torque ceiling that caps the top of the resonance map (which speeds are
quiet vs ringy — that map is `find-speed`). The usual real print-speed limit is the
hotend's flow rate, which is a thermal, not a motion, measurement.
"""
from __future__ import annotations

import json
import math
import os

from .collect import Screen, detect_hardware, enter_spreadcycle, exit_spreadcycle, refuse_if_printing, run_restore
from .current import Referee, referee_axis, stress_vector
from .klippy import Klippy, find_socket

STATE = os.path.expanduser('~/printer_data/config/chopper-autotune/envelope.json')


def ceiling_label(hold, skip, kilo: bool = False) -> str:
    """350+ = held the whole tested range; 300 = the last safe rung before a skip;
    <150 = skipped already at the first rung."""
    fmt = (lambda v: '%gk' % (v / 1000)) if kilo else (lambda v: '%g' % v)
    if hold is None:
        return '<%s' % fmt(skip)
    return fmt(hold) + ('' if skip is not None else '+')


def save_state(results: 'dict[str, dict]'):
    """Remember the measured ceilings so the panel's Results can show the achieved
    speed/acceleration at any time."""
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, 'w') as handle:
            json.dump(results, handle)
    except OSError:
        pass

MARGIN = 1.3                                  # recommend ceiling / margin
STRESS_REPS = 3
SKIP_HEAD_MM = 0.6                            # head offset that counts as a lost step (~0.8 mm quantum)


def stress_burst(kl: Klippy, board, motor: str, vec: 'tuple[float, float]',
                 speed: float, accel: float, span: float):
    """One single-motor stress burst at (speed, accel): a diagonal that loads only this
    motor on coupled-XY, a few back-and-forth passes, net-zero."""
    cx, cy = board.center
    feed = speed / math.hypot(*vec) * 60.0
    kl.gcode('G90\nM204 S%.0f\nG1 X%.1f Y%.1f F6000\nM400' % (accel, cx, cy))
    moves = []
    for _ in range(STRESS_REPS):
        moves += ['G1 X%.1f Y%.1f F%.0f' % (cx + span * vec[0], cy + span * vec[1], feed),
                  'G1 X%.1f Y%.1f F%.0f' % (cx - span * vec[0], cy - span * vec[1], feed)]
    moves.append('G1 X%.1f Y%.1f F6000' % (cx, cy))
    kl.gcode('\n'.join(moves) + '\nM400')


def ceiling(ladder, run_one, report):
    """Walk a rising ladder; the safe ceiling is the last rung before the first skip."""
    held = None
    for value in ladder:
        skipped = run_one(value)
        report(value, skipped)
        if skipped:
            return held, value
        held = value
    return held, None                          # never skipped in the tested range


def verdict(hold, skip, unit: str) -> str:
    if hold is None:
        return 'skips already at the lowest tested value — margin is too thin'
    if skip is None:
        return 'no skip through the tested range (to %g %s) — the motor is not the limit here' % (
            hold, unit)
    return 'holds to %g %s (skips at %g); stay under %g %s (%.1fx margin)' % (
        hold, unit, skip, hold / MARGIN, unit, MARGIN)


def run_envelope(args) -> int:
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        return envelope(kl, args)
    finally:
        kl.close()


def envelope(kl: Klippy, args) -> int:
    from .collect import motor_label
    motors = ['x', 'y'] if args.axis == 'xy' else [args.axis]
    hw = {m: detect_hardware(kl, m) for m in motors}
    board = hw[motors[0]]
    settings = kl.settings()
    base_accel = args.accel or board.max_accel
    speeds = tuple(range(args.min_speed, args.max_speed + 1, args.step))
    accels = tuple(round(base_accel * f, -2) for f in (1.0, 1.5, 2.0, 3.0, 4.0))

    print('Motion envelope on motor(s) %s at the configured run current: worst-case '
          'single-motor stress, endstop referee.' % '+'.join(motor_label(m) for m in motors))
    print('  speed ladder %s mm/s (accel %.0f); accel ladder %s mm/s2 (speed %d)'
          % ('/'.join(map(str, speeds)), base_accel, '/'.join('%g' % a for a in accels),
             args.accel_probe_speed))
    if args.dry_run:
        return 0
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1

    refuse_if_printing(kl)
    screen = Screen(kl, board.display)
    achieved = {}
    try:
        kl.gcode('G28 X Y\nG90')
        for m in motors:
            label = motor_label(m)
            span = min(25.0, hw[m].axis_span / 8)
            vec = stress_vector(board.kinematics, m)
            current = float(settings['tmc%s stepper_%s' % (hw[m].driver.name, m)]['run_current'])
            print('\n=== Motor %s @ %.2f A ===' % (label, current))

            def skips(slip):
                return slip is None or abs(slip) > SKIP_HEAD_MM

            def report(value, skipped, unit, label=label):
                screen.update('Chopper envelope %s %g%s' % (label, value, unit), force=True)
                print('   %-8g %-6s : %s' % (value, unit, 'SLIP' if skipped else 'holds'))

            enter_spreadcycle(kl, hw[m])
            try:
                ref = Referee(kl, referee_axis(board.kinematics, m), settings,
                              board.center[1] if referee_axis(board.kinematics, m) == 'x'
                              else board.center[0])
                ref.calibrate()
                print(' speed ceiling (accel %.0f):' % base_accel)
                s_hold, s_skip = ceiling(
                    speeds,
                    lambda v: stress_burst(kl, board, m, vec, v, base_accel, span) or skips(ref.slipped()),
                    lambda v, sk: report(v, sk, 'mm/s'))
                print(' accel ceiling (speed %d mm/s):' % args.accel_probe_speed)
                a_hold, a_skip = ceiling(
                    accels,
                    lambda a: stress_burst(kl, board, m, vec, args.accel_probe_speed, a, span) or skips(ref.slipped()),
                    lambda a, sk: report(a, sk, 'mm/s2'))
            finally:
                run_restore(lambda: kl.gcode('M204 S%.0f' % board.max_accel),
                            lambda mm=m: exit_spreadcycle(kl, hw[mm]))
            print(' => speed: %s' % verdict(s_hold, s_skip, 'mm/s'))
            print(' => accel: %s' % verdict(a_hold, a_skip, 'mm/s2'))
            achieved[label] = {'speed': ceiling_label(s_hold, s_skip),
                               'accel': ceiling_label(a_hold, a_skip, kilo=True)}
    finally:
        run_restore(lambda: kl.gcode('M204 S%.0f\nG28 X Y' % board.max_accel))

    if achieved:
        save_state(achieved)                        # the panel's Results shows these
        screen.final('Envelope: ' + ' · '.join(
            '%s %s mm/s, %s acc' % (label, values['speed'], values['accel'])
            for label, values in achieved.items()))
    print('\nThis is the motor (torque) limit only. For which speeds are quiet vs ringy, '
          'run CHOPPER_FIND_SPEED; and the real top-speed limit is usually the hotend flow '
          'rate, which is not a motion measurement.')
    return 0
