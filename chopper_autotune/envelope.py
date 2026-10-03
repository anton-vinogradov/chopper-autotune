"""Motor motion envelope: how fast and how hard each motor can be pushed before it
loses steps, at the configured run current, via the endstop referee.

This is the torque ceiling that caps the top of the resonance map (which speeds are
quiet vs ringy — that map is `find-speed`). The usual real print-speed limit is the
hotend's flow rate, which is a thermal, not a motion, measurement.
"""
from __future__ import annotations

import math
import os

from .collect import (KLIPPY_DIR, Screen, ThermalGuard, coupled_xy, detect_hardware,
                      enter_spreadcycle, exit_spreadcycle, fit_row, full_steps_per_mm, home_xy,
                      rail_twins, refuse_blind_z_hop, refuse_corexz, refuse_if_printing,
                      rehome_unless_hot, run_restore)
from .current import (Referee, accel_along, axis_limits, belt_cap, belt_top, free_strokes,
                      keep_axis_limits, lift_axis_limits, live_limits, referee_axis, stress_vector,
                      stroke_accel, stroke_peak, velocity_caps, velocity_needs)
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


CRISP_SMOOTHING = 0.05      # Klipper docs' "less smoothing" band; their default target
                            # (0.12 mm, hardcoded in find_shaper_max_accel) = plain good


def shaper_accels(settings) -> 'dict[str, tuple[str, float, int, int]]':
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
                good = int(helper.find_shaper_max_accel(impulses, scv))
                try:
                    crisp = int(helper._bisect(
                        lambda accel: helper._get_shaper_smoothing(impulses, accel, scv)
                        <= CRISP_SMOOTHING))
                except AttributeError:              # private API moved: keep the good number
                    crisp = None
                out[axis] = (name, freq, good, crisp)
        return out
    except Exception as why:                       # any klippy-version surprise: no cap,
        print('note: input-shaper cap unavailable (%s)' % why)
        return {}                                  # the motor numbers still stand


def recommend_limits(speed_holds: 'dict[str, float]', accel_holds: 'dict[str, float]',
                     kinematics: str, shaper: 'dict[str, tuple[str, float, int]]',
                     printer_now: 'dict[str, float]', margin: float = MARGIN) -> 'dict | None':
    """The "what to set" numbers, separated by WHERE they go — conflating them read as
    a downgrade in the field (a 7300 smoothing threshold next to a configured 10000):

    - [printer] max_velocity: any-direction belt safety. On coupled XY a pure X/Y move
      runs BOTH belts at head speed and a 45 degree one runs a belt sqrt(2) faster, so
      the cap is tested-ceiling/sqrt(2) — except on Kalico's limited_corexy, where
      max_velocity caps the belt itself; the /margin variant rides along.
    - [printer] max_accel: the MACHINE cap (motor torque/margin) — travels use it, and
      smoothing costs nothing where no plastic is laid.
    - on Kalico's limited_cartesian each motor drives its own axis and has its own limits
      (max_x_velocity, max_y_accel...); Kalico refuses one above max_velocity/max_accel
      and pairs those as the hypotenuse of the two axes.
    - slicer print accel: the input shaper's smoothing threshold — print quality only."""
    if not speed_holds or None in speed_holds.values() \
            or not accel_holds or None in accel_holds.values():
        return None                                # skipped at the first rung: fix that first
    belt = min(speed_holds.values())
    vel = belt / math.sqrt(2) if coupled_xy(kinematics) and kinematics != 'limited_corexy' else belt
    machine_accel = int(min(accel_holds.values()) / margin // 100 * 100)
    per_axis = None
    if kinematics == 'limited_cartesian':
        per_axis = {}
        for axis, label in (('x', 'A'), ('y', 'B')):
            per_axis['max_%s_velocity' % axis] = int(speed_holds[label])
            per_axis['max_%s_accel' % axis] = int(accel_holds[label] / margin // 100 * 100)
        vel = math.hypot(per_axis['max_x_velocity'], per_axis['max_y_velocity'])
        machine_accel = int(math.hypot(per_axis['max_x_accel'], per_axis['max_y_accel']) // 100 * 100)
    print_accel = min((good for _, _, good, _ in shaper.values()), default=None)
    crisp_accels = [crisp for _, _, _, crisp in shaper.values() if crisp]
    crisp_accel = min(crisp_accels) if crisp_accels else None
    slowest_shaper = min(shaper, key=lambda a: shaper[a][2]) if shaper else None
    return {'max_velocity': int(vel), 'max_velocity_margin': int(vel / margin),
            'belt_ceiling': belt,
            'max_accel': machine_accel,
            'print_accel': int(print_accel // 100 * 100) if print_accel else None,
            'print_accel_crisp': int(crisp_accel // 100 * 100) if crisp_accel else None,
            'limited_by': ('%s shaper (%s@%.1f)' % (slowest_shaper.upper(),
                                                    *shaper[slowest_shaper][:2])
                           if slowest_shaper else None),
            'now_velocity': printer_now.get('max_velocity'),
            'now_accel': printer_now.get('max_accel'),
            'per_axis': per_axis}


def verdict_now(now: 'float | None', suggested: float, over: str, ok: str = 'ok') -> str:
    if not now:
        return ''
    return ' (now %g — %s)' % (now, ok if now <= suggested else over)


STRESS_REPS = 3
SKIP_HEAD_MM = 0.6                            # head offset that counts as a lost step (~0.8 mm quantum)


def stress_burst(kl: Klippy, board, motor: str, vec: 'tuple[float, float]',
                 speed: float, accel: float, span: float, check=lambda: None):
    """One single-motor stress burst at (speed, accel): a diagonal that loads only this
    motor on coupled-XY, a few back-and-forth passes, net-zero. check() runs before
    every pass (see current.run_rung)."""
    cx, cy = board.center
    feed = speed / math.hypot(*vec) * 60.0
    check()
    # from one end: the first stroke runs the full 2*span too
    kl.gcode('G90\nM204 S%.0f\nG1 X%.1f Y%.1f F6000\nM400'
             % (accel, cx - span * vec[0], cy - span * vec[1]))
    for _ in range(STRESS_REPS):
        check()
        kl.gcode('G1 X%.1f Y%.1f F%.0f\nG1 X%.1f Y%.1f F%.0f\nM400'
                 % (cx + span * vec[0], cy + span * vec[1], feed,
                    cx - span * vec[0], cy - span * vec[1], feed))
    kl.gcode('G1 X%.1f Y%.1f F6000\nM400' % (cx, cy))


def stroke_ladders(kinematics: str, motor: str, axis_span: float, speeds, accel: float, accels,
                   probe_speed: float, max_velocity: float, configured: float = 0,
                   per_axis: 'dict | None' = None):
    """(span, vec, speed rungs, accel rungs, why the speed ladder stops short or None) of one
    motor, without the rungs its strokes cannot run: a rung never reached would 'hold'
    and top the advice. The rungs are belt speeds; max_velocity is the one in force,
    configured the one in printer.cfg, per_axis Kalico's limits in force (axis_limits)."""
    from .collect import motor_label
    label = motor_label(motor)
    span = min(25.0, axis_span / 8)
    vec = stress_vector(kinematics, motor)
    caps = velocity_caps(kinematics, vec, max_velocity, per_axis)
    cap = belt_cap(caps)
    top = belt_top(span, vec, accel, cap)
    kept = tuple(speed for speed in speeds if speed <= top)
    source = (' (set at runtime; printer.cfg has %g)' % configured
              if configured and configured != max_velocity else '')
    held_by = ', '.join(['max_velocity %g%s' % (max_velocity, source)]
                        + ['%s %g' % (option, value) for option, (value, _) in caps.items()
                           if option != 'max_velocity'] + ['accel %.0f' % accel])

    def needs(speed) -> str:
        # every limit that stops a stroke short of this belt speed, not just the lower one
        return ' and '.join(velocity_needs(caps, speed)
                            + (['ACCEL to %d' % stroke_accel(speed, span, vec)]
                               if stroke_peak(span, vec, accel) < speed else []))
    if not kept:
        # the action first: the display shows its first characters (failure_display)
        raise SystemExit('raise %s or more: motor %s runs under %d mm/s, MIN_SPEED %d (%s). '
                         'Nothing was moved'
                         % (needs(speeds[0]), label, math.floor(top) + 1, speeds[0], held_by))
    short = None
    if len(kept) < len(speeds):
        short = ('the strokes stop at %d mm/s (%s): raise %s to reach %d'
                 % (math.floor(top), held_by, needs(speeds[-1]), speeds[-1]))
        print('Motor %s: the speed ladder stops at %d mm/s: %s' % (label, kept[-1], short))
    kept_accels = tuple(a for a in accels if probe_speed <= belt_top(span, vec, a, cap))
    if not kept_accels:
        raise SystemExit('lower ACCEL_PROBE_SPEED to %d or less: no accel rung up to %g lets '
                         'motor %s reach %d mm/s. Nothing was moved'
                         % (belt_top(span, vec, accels[-1], cap), accels[-1], label,
                            probe_speed))
    if len(kept_accels) < len(accels):
        print('Motor %s: the accel ladder starts at %g mm/s2: below it a stroke never reaches '
              'the %d mm/s probe speed' % (label, kept_accels[0], probe_speed))
    return span, vec, kept, kept_accels, short


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


def awd_note(settings: dict, motors: 'list[str]') -> str:
    """The moves turn both motors of a pair, but spreadCycle reaches stepper_x/y only.
    The run is detached: the note must reach the display and the result, not just the log."""
    twins = [name for motor in motors for name in rail_twins(settings, motor)]
    return ('%s share an axis: spreadCycle forced on stepper_x/stepper_y only, the verdict '
            'is approximate (issue #129)' % ', '.join(twins)) if twins else ''


def envelope(kl: Klippy, args) -> int:
    from .collect import motor_label
    motors = ['x', 'y'] if args.axis == 'xy' else [args.axis]
    hw = {m: detect_hardware(kl, m, accel=False) for m in motors}
    board = hw[motors[0]]
    settings = kl.settings()
    refuse_corexz(settings)                 # AWD runs, approximate: no refuse_multi_motor
    note = awd_note(settings, motors)
    if note:
        print('WARNING: ' + note)
    speeds = tuple(range(args.min_speed, args.max_speed + 1, args.step))
    # G1 feed is clamped to max_velocity, the one in force now: rungs above it would "hold"
    # without ever being run, as would rungs a stroke is too short to reach
    limits = live_limits(kl)
    per_axis = axis_limits(kl, board.kinematics)
    configured = float(settings.get('printer', {}).get('max_velocity') or 0)
    # the speed ladder at the printer's own accel: on Kalico's limited_* kinematics what
    # its per-axis limits give the strokes of this motor
    bases = {m: args.accel or accel_along(board.kinematics, stress_vector(board.kinematics, m),
                                          board.max_accel, per_axis) for m in motors}
    ladders = {m: stroke_ladders(board.kinematics, m, hw[m].axis_span, speeds, bases[m],
                                 tuple(round(bases[m] * f, -2) for f in (1.0, 1.5, 2.0, 3.0, 4.0)),
                                 args.accel_probe_speed, limits['max_velocity'], configured,
                                 per_axis)
               for m in motors}

    print('Motion envelope on motor(s) %s at the configured run current: worst-case '
          'single-motor stress, endstop referee.' % '+'.join(motor_label(m) for m in motors))
    for m in motors:
        _, _, motor_speeds, motor_accels, _ = ladders[m]
        capped = (', what the per-axis limits give it of max_accel %g' % board.max_accel
                  if not args.accel and bases[m] < board.max_accel else '')
        print('  motor %s: speed ladder %s mm/s (accel %.0f%s); accel ladder %s mm/s2 (speed %d)'
              % (motor_label(m), '/'.join(map(str, motor_speeds)), bases[m], capped,
                 '/'.join('%g' % a for a in motor_accels), args.accel_probe_speed))
    top_speed = max(ladders[m][2][-1] for m in motors)
    rail = settings.get('stepper_%s' % motors[0], {})
    if rail.get('rotation_distance') and rail.get('microsteps'):
        # the ladder's real ceiling is usually the MCU's step generation, not the motor:
        # show the step rate so a Klipper "step rate" shutdown is no surprise
        steps_per_mm = full_steps_per_mm(rail) * int(rail['microsteps'])
        print('  ladder top %d mm/s = %.0fk steps/s at %sx microstepping — if Klipper '
              'shuts down on step rate, lower MAX_SPEED'
              % (top_speed, top_speed * steps_per_mm / 1000, rail['microsteps']))
    if args.dry_run:
        return 0
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1

    refuse_if_printing(kl)
    screen = Screen(kl, board.display)
    guard = ThermalGuard(kl, settings)
    if note:
        screen.update('WARNING: ' + note, force=True, short='AWD: approx.')
    achieved = {}
    speed_holds, accel_holds = {}, {}
    refuse_blind_z_hop(kl, settings)
    guard.preflight()
    restores, shorts, skipped = [], {}, False
    try:
        home_xy(kl, 'G28 X Y\nG90')
        free_strokes(kl, settings, limits, restores)
        keep_axis_limits(kl, per_axis, restores)
        for m in motors:
            label = motor_label(m)
            lift_axis_limits(kl, board.kinematics, per_axis, m)
            span, vec, motor_speeds, motor_accels, short = ladders[m]
            current = float(settings['tmc%s stepper_%s' % (hw[m].driver.name, m)]['run_current'])
            print('\n=== Motor %s @ %.2f A ===' % (label, current))

            def skips(slip):
                return slip is None or abs(slip) > SKIP_HEAD_MM

            def report(value, skipped, unit, label=label):
                screen.update('Chopper envelope %s %g%s' % (label, value, unit), force=True,
                              short='%s %g%s' % (label, value, unit))
                print('   %-8g %-6s : %s' % (value, unit, 'SLIP' if skipped else 'holds'))

            enter_spreadcycle(kl, hw[m], restores=False)
            try:
                ref = Referee(kl, referee_axis(board.kinematics, m), settings,
                              board.center[1] if referee_axis(board.kinematics, m) == 'x'
                              else board.center[0])
                guard.check()
                ref.calibrate()
                print(' speed ceiling (accel %.0f):' % bases[m])
                s_hold, s_skip = ceiling(
                    motor_speeds,
                    lambda v: stress_burst(kl, board, m, vec, v, bases[m], span, guard.check)
                    or guard.check() or skips(ref.slipped()),
                    lambda v, sk: report(v, sk, 'mm/s'))
                print(' accel ceiling (speed %d mm/s):' % args.accel_probe_speed)
                a_hold, a_skip = ceiling(
                    motor_accels,
                    lambda a: stress_burst(kl, board, m, vec, args.accel_probe_speed, a, span, guard.check)
                    or guard.check() or skips(ref.slipped()),
                    lambda a, sk: report(a, sk, 'mm/s2'))
            finally:
                run_restore(lambda: kl.gcode('M204 S%.0f' % limits['max_accel']),
                            lambda mm=m: exit_spreadcycle(kl, hw[mm]))
            print(' => speed: %s' % verdict(s_hold, s_skip, 'mm/s'))
            print(' => accel: %s' % verdict(a_hold, a_skip, 'mm/s2'))
            achieved[label] = {'speed': ceiling_label(s_hold, s_skip),
                               'accel': ceiling_label(a_hold, a_skip, kilo=True)}
            speed_holds[label], accel_holds[label] = s_hold, a_hold
            skipped = skipped or s_skip is not None
            if short:
                shorts[label] = short
    finally:
        run_restore(lambda: kl.gcode('M204 S%.0f' % limits['max_accel']), *restores,
                    lambda: rehome_unless_hot(kl))

    finale = short = ''
    if achieved:
        save_state(achieved)                        # the panel's Results shows these
        finale = 'Envelope: ' + ' · '.join(
            '%s %s mm/s, %s acc' % (label, values['speed'], values['accel'])
            for label, values in achieved.items())
        short = fit_row('vel', ['%s%s' % (label, values['speed'])
                                for label, values in achieved.items()])

    # no motor skipped and the slowest ladder stopped short: the test ran out, not the motor
    untested = {} if skipped or not speed_holds else {
        label: short for label, short in shorts.items()
        if speed_holds[label] == min(speed_holds.values())}
    recommendation, shaper_caps = None, {}
    if args.axis == 'xy':                           # both motors measured in THIS run
        shaper_caps = shaper_accels(settings)
        recommendation = recommend_limits(speed_holds, accel_holds, board.kinematics, shaper_caps,
                                          settings.get('printer', {}))
    if recommendation:
        if note:
            recommendation['approximate'] = 'AWD'
        if untested:
            recommendation['untested'] = sorted(untested)
        save_state({'recommend': recommendation})   # Results carries the numbers
        rec = recommendation
        print('\n=== What to set ===')
        printer_now = settings.get('printer', {})
        if rec['per_axis']:
            axes = rec['per_axis']
            print('[printer] %s\n    each axis stays at its motor\'s tested belt ceiling; %s keep a '
                  '%.1fx margin'
                  % (', '.join('max_%s_velocity: %d%s' % (
                      axis, axes['max_%s_velocity' % axis],
                      verdict_now(printer_now.get('max_%s_velocity' % axis), axes['max_%s_velocity' % axis],
                                  over='untested above, not a limit' if untested
                                  else 'its motor would outrun the tested ceiling'))
                               for axis in 'xy'),
                     ' and '.join('%d' % (axes['max_%s_velocity' % axis] / MARGIN) for axis in 'xy'),
                     MARGIN))
        else:
            if board.kinematics == 'limited_corexy':
                why, outrun = 'Kalico caps each belt by max_velocity here', 'any travel'
            elif coupled_xy(board.kinematics):
                why, outrun = 'a 45 deg move runs a belt sqrt(2) faster than the head', 'a 45 deg travel'
            else:
                why, outrun = 'an X or Y move runs its belt at head speed', 'an X or Y travel'
            print('[printer] max_velocity: %d%s\n    every direction stays at the tested %g mm/s'
                  ' belt ceiling (%s); %d keeps a %.1fx margin'
                  % (rec['max_velocity'],
                     verdict_now(rec['now_velocity'], rec['max_velocity'],
                                 over='untested above, not a limit' if untested
                                 else outrun + ' would outrun the tested ceiling'),
                     rec['belt_ceiling'], why, rec['max_velocity_margin'], MARGIN))
        for label, why in untested.items():
            print('    untested above %g mm/s on motor %s: %s' % (speed_holds[label], label, why))
        if rec['per_axis']:
            print('[printer] %s\n    each motor\'s torque /%.1f'
                  % (', '.join('max_%s_accel: %d%s' % (
                      axis, axes['max_%s_accel' % axis],
                      verdict_now(printer_now.get('max_%s_accel' % axis), axes['max_%s_accel' % axis],
                                  over='above the motor margin'))
                               for axis in 'xy'), MARGIN))
            print('[printer] max_velocity: %d, max_accel: %d\n    the hypotenuse of the per-axis '
                  'values, as Kalico pairs them: a diagonal uses both axes, and Kalico refuses a '
                  'per-axis value above these' % (rec['max_velocity'], rec['max_accel']))
        else:
            print('[printer] max_accel: %d%s\n    the MACHINE cap: motor torque /%.1f;'
                  ' travels use it — smoothing costs nothing where no plastic is laid'
                  % (rec['max_accel'],
                     verdict_now(rec['now_accel'], rec['max_accel'],
                                 over='above the motor margin'), MARGIN))
        from .resonance_map import STATE as MAP_STATE
        from .dataset import load_json
        vfa = load_json(MAP_STATE)
        motors_map = {m: v for m, v in vfa.items() if isinstance(v, dict) and 'dips' in v}
        if motors_map:
            dips = sorted({s for v in motors_map.values() for s in v.get('dips', [])})
            peaks = sorted({s for v in motors_map.values() for s in v.get('peaks', [])})
            advice = '; '.join('%s' % v['advice'] for v in motors_map.values() if v.get('advice'))
            print('slicer print speed: good quality = whatever the hotend flow allows (the '
                  'belts hold %g+ mm/s, VFA banding is the only motion-side cost); crisp '
                  'walls = cruise in the dips (%s), avoid the VFA peaks (%s)%s'
                  % (rec['belt_ceiling'], ', '.join(map(str, dips)) or '?',
                     ', '.join(map(str, peaks)) or '-', ' — %s' % advice if advice else ''))
        else:
            print('slicer print speed: good quality = whatever the hotend flow allows (the '
                  'belts hold %g+ mm/s); for crisp walls run CHOPPER_MAP PRINT_SPEED=<yours> '
                  '(which cruise speeds ring = VFA)' % rec['belt_ceiling'])
        if rec['print_accel']:
            crisp = (' (crisp detail: <=%d, smoothing under %.2f mm)'
                     % (rec['print_accel_crisp'], CRISP_SMOOTHING)
                     if rec.get('print_accel_crisp') else '')
            print('slicer print accel: <=%d for good quality%s\n    %s smoothing passes'
                  ' Klipper\'s 0.12 mm guidance above this — print-move quality only'
                  % (rec['print_accel'], crisp, rec['limited_by']))
        else:
            print('(no [input_shaper] found — run SHAPER_CALIBRATE for the print-accel guidance)')
        finale += ' · %s max vel %d acc %dk print %.1fk' % (
            'tested' if untested else 'set', rec['max_velocity'], rec['max_accel'] / 1000,
            (rec['print_accel'] or 0) / 1000)
    if finale and note:
        finale += ' · AWD: approximate (#129)'
    if finale:
        screen.final(finale, short)
    print('\nThis is the motor (torque) limit only. For which speeds are quiet vs ringy, '
          'run CHOPPER_FIND_SPEED; and the real top-speed limit is usually the hotend flow '
          'rate, which is not a motion measurement.')
    return 0
