"""Motor motion envelope: how fast and how hard each motor can be pushed before it
loses steps, at the configured run current, via the endstop referee.

This is the torque ceiling that caps the top of the resonance map (which speeds are
quiet vs ringy — that map is `find-speed`). The usual real print-speed limit is the
hotend's flow rate, which is a thermal, not a motion, measurement.
"""
from __future__ import annotations

import math
import os

from .collect import Screen, detect_hardware, enter_spreadcycle, exit_spreadcycle, refuse_if_printing, run_restore
from .current import Referee, referee_axis, stress_vector
from .dataset import save_json
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
    speed/acceleration at any time. Merged per motor: MOTOR=B must not erase A."""
    save_json(STATE, results, merge=True)

MARGIN = 1.3                                  # recommend ceiling / margin
KLIPPY_DIR = os.path.expanduser('~/klipper/klippy')


def shaper_accels(settings) -> 'dict[str, tuple[str, float, int]]':
    """Klipper's own suggested max_accel per axis for the configured [input_shaper],
    computed by importing the very shaper code this printer runs — no reimplemented
    formula to drift (and none to copy: klippy is GPL, this repo is not)."""
    shaper = settings.get('input_shaper')
    if not shaper or not os.path.isdir(KLIPPY_DIR):
        return {}
    try:
        import sys
        if KLIPPY_DIR not in sys.path:
            sys.path.insert(0, KLIPPY_DIR)
        from extras import shaper_calibrate, shaper_defs
        helper = shaper_calibrate.ShaperCalibrate(printer=None)
        scv = float(settings.get('printer', {}).get('square_corner_velocity', 5.0))
        out = {}
        for axis in ('x', 'y'):
            name = shaper.get('shaper_type_%s' % axis) or shaper.get('shaper_type')
            freq = float(shaper.get('shaper_freq_%s' % axis) or 0)
            cfg = next((s for s in shaper_defs.INPUT_SHAPERS if s.name == name), None)
            if cfg and freq:
                impulses = cfg.init_func(freq, shaper_defs.DEFAULT_DAMPING_RATIO)
                out[axis] = (name, freq, int(helper.find_shaper_max_accel(impulses, scv)))
        return out
    except Exception as why:                       # any klippy-version surprise: no cap,
        print('note: input-shaper cap unavailable (%s)' % why)
        return {}                                  # the motor numbers still stand


def recommend_limits(speed_holds: 'dict[str, float]', accel_holds: 'dict[str, float]',
                     coupled: bool, shaper: 'dict[str, tuple[str, float, int]]',
                     margin: float = MARGIN) -> 'dict | None':
    """[printer] numbers from everything measured: velocity from the slowest motor's
    belt ceiling (on coupled XY a pure X/Y move runs BOTH belts at head speed, and a 45
    degree diagonal runs one belt sqrt(2) faster — the bulletproof number covers that);
    accel = min(motor ceiling, the input shaper's own suggestion) — ringing usually wins."""
    if not speed_holds or None in speed_holds.values() \
            or not accel_holds or None in accel_holds.values():
        return None                                # skipped at the first rung: fix that first
    belt = min(speed_holds.values())
    vel_axis = belt / margin
    vel = vel_axis / math.sqrt(2) if coupled else vel_axis
    caps = {'motor torque': min(accel_holds.values()) / margin}
    for axis, (name, freq, accel) in shaper.items():
        caps['%s shaper (%s@%.1f)' % (axis.upper(), name, freq)] = accel
    limited_by = min(caps, key=caps.get)
    return {'max_velocity': int(vel), 'max_velocity_axis': int(vel_axis),
            'max_accel': int(caps[limited_by] // 100 * 100), 'limited_by': limited_by}


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
    max_velocity = float(settings.get('printer', {}).get('max_velocity') or 0)
    if max_velocity and speeds and speeds[-1] > max_velocity:
        # G1 feed is silently clamped to [printer] max_velocity — rungs above it would
        # "hold" without ever being commanded, so cut them instead of lying
        speeds = tuple(s for s in speeds if s <= max_velocity)
        print('Capping the speed ladder at [printer] max_velocity = %g mm/s — raise it in '
              'the config to probe higher' % max_velocity)
        if not speeds:
            raise SystemExit('MIN_SPEED %d exceeds [printer] max_velocity %g — nothing to test'
                             % (args.min_speed, max_velocity))

    print('Motion envelope on motor(s) %s at the configured run current: worst-case '
          'single-motor stress, endstop referee.' % '+'.join(motor_label(m) for m in motors))
    print('  speed ladder %s mm/s (accel %.0f); accel ladder %s mm/s2 (speed %d)'
          % ('/'.join(map(str, speeds)), base_accel, '/'.join('%g' % a for a in accels),
             args.accel_probe_speed))
    rail = settings.get('stepper_%s' % motors[0], {})
    if rail.get('rotation_distance') and rail.get('microsteps'):
        # the ladder's real ceiling is usually the MCU's step generation, not the motor:
        # show the step rate so a Klipper "step rate" shutdown is no surprise
        steps_per_mm = 200 * int(rail['microsteps']) / float(rail['rotation_distance'])
        print('  ladder top %d mm/s = %.0fk steps/s at %sx microstepping — if Klipper '
              'shuts down on step rate, lower MAX_SPEED'
              % (speeds[-1], speeds[-1] * steps_per_mm / 1000, rail['microsteps']))
    if args.dry_run:
        return 0
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1

    refuse_if_printing(kl)
    screen = Screen(kl, board.display)
    achieved = {}
    speed_holds, accel_holds = {}, {}
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
            speed_holds[label], accel_holds[label] = s_hold, a_hold
    finally:
        run_restore(lambda: kl.gcode('M204 S%.0f\nG28 X Y' % board.max_accel))

    finale = ''
    if achieved:
        save_state(achieved)                        # the panel's Results shows these
        finale = 'Envelope: ' + ' · '.join(
            '%s %s mm/s, %s acc' % (label, values['speed'], values['accel'])
            for label, values in achieved.items())

    recommendation, shaper_caps = None, {}
    if args.axis == 'xy':                           # both motors measured in THIS run
        from .collect import coupled_xy
        shaper_caps = shaper_accels(settings)
        recommendation = recommend_limits(speed_holds, accel_holds,
                                          coupled_xy(board.kinematics), shaper_caps)
    if recommendation:
        save_state({'recommend': recommendation})   # Results carries the numbers
        print('\n=== Recommended [printer] limits ===')
        print('max_velocity: %d   # slowest belt holds %g+ mm/s, /%.1f margin%s'
              % (recommendation['max_velocity'], min(speed_holds.values()), MARGIN,
                 ', /sqrt(2) for coupled-XY diagonals (%d for pure X/Y moves)'
                 % recommendation['max_velocity_axis']
                 if recommendation['max_velocity'] != recommendation['max_velocity_axis'] else ''))
        print('max_accel: %d     # limited by %s'
              % (recommendation['max_accel'], recommendation['limited_by']))
        if not shaper_caps:
            print('(no [input_shaper] found — run SHAPER_CALIBRATE for the ringing-side cap)')
        finale += ' · rec <=%d mm/s, <=%.1fk acc' % (recommendation['max_velocity'],
                                                     recommendation['max_accel'] / 1000)
    if finale:
        screen.final(finale)
    print('\nThis is the motor (torque) limit only. For which speeds are quiet vs ringy, '
          'run CHOPPER_FIND_SPEED; and the real top-speed limit is usually the hotend flow '
          'rate, which is not a motion measurement.')
    return 0
