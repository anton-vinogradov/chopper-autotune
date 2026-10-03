"""Run-current tuning: find the minimal current that survives a worst-case stress
pattern, with an endstop referee catching the skipped steps.

The referee is the judge, not the accelerometer: a stall can be nearly silent
(measured — the default chopper at 0.65 A lost 14.8 mm with no roar), but skipped
steps always land as a position offset, and the endstop cannot be fooled. A skip
is quantized to one electrical cycle (4 full steps ≈ 0.8 mm of belt), far above
the 0.2 mm creep resolution.
"""
from __future__ import annotations

import math
import os
import re
import sys

from .collect import (Screen, ThermalGuard, coupled_xy, detect_hardware, home_xy,
                      refuse_blind_z_hop, refuse_if_printing, refuse_multi_motor, rehome_unless_hot,
                      run_restore)
from .dataset import save_json
from .klippy import Klippy, find_socket

BELT_SPEEDS = (100, 150, 200)
STATE = os.path.expanduser('~/printer_data/config/chopper-autotune/current.json')
STROKES_PER_SPEED = 3
COARSE_STEP = 2.0          # fast approach step; the fine creep only covers the last one
CREEP_STEP = 0.2          # fine step near the trigger, for the actual precision
CREEP_START = 12.0         # true distance from the endstop before a creep
CREEP_RANGE = 28.0         # how far the SET_KINEMATIC_POSITION lie lets us travel
SLIP_HEAD_MM = 0.3         # threshold on the head offset; one slip quantum is ~0.8


def stress_vector(kinematics: str, motor: str) -> 'tuple[float, float]':
    """Head direction that loads ONLY the given motor: on coupled-XY kinematics a
    pure X move splits the load between both motors, so the single-motor stress is
    the X=Y (motor A) / X=-Y (motor B) diagonal; on Cartesian each motor owns its axis."""
    if coupled_xy(kinematics):
        return (1.0, 1.0) if motor == 'x' else (1.0, -1.0)
    return (1.0, 0.0) if motor == 'x' else (0.0, 1.0)


def referee_axis(kinematics: str, motor: str) -> str:
    """A slipped motor shifts the head; on coupled-XY either endstop sees half the
    belt slip, so the X endstop serves both motors."""
    return 'x' if coupled_xy(kinematics) or motor == 'x' else 'y'


def bisect_threshold(holds, lo: float, hi: float, resolution: float) -> float:
    """Smallest verified holding current: hi is known to hold, lo known to slip."""
    while hi - lo > resolution:
        mid = round((lo + hi) / 2, 3)
        if holds(mid):
            hi = mid
        else:
            lo = mid
    return hi


class Referee:
    """Position-loss detector: creep toward an endstop in CREEP_STEP moves polling
    QUERY_ENDSTOPS; the trigger distance vs the expected one is the head offset.
    SET_KINEMATIC_POSITION widens the legal travel so offsets of either sign are
    measurable up to ~±13 mm; a per-run calibration absorbs the systematic bias."""

    def __init__(self, kl: Klippy, axis: str, settings: dict, park_other: float):
        rail = settings['stepper_' + axis]
        endstop_pin = str(rail.get('endstop_pin') or '')
        if 'virtual_endstop' in endstop_pin:
            # sensorless homing: the "endstop" is StallGuard, which needs sustained
            # velocity to detect a stall — the referee's slow creep would never trigger
            # it (or trigger it randomly). Refuse honestly instead of measuring noise.
            raise SystemExit('the %s endstop is sensorless (%s) — the endstop referee '
                             'needs a physical switch; CHOPPER_CURRENT/CHOPPER_ENVELOPE '
                             'are not available on this machine (the accelerometer tools '
                             'all work)' % (axis, endstop_pin))
        self.kl = kl
        self.axis = axis
        self.endstop = float(rail['position_endstop'])
        mid = (float(rail.get('position_min', 0.0)) + float(rail['position_max'])) / 2
        self.home_dir = 1.0 if self.endstop > mid else -1.0
        self.park_other = park_other
        self.bias = 0.0
        self.key = None

    def _endstop_key(self) -> str:
        """The rail's endstop in query_endstops/status: 'x' on every release and in Kalico,
        'stepper_x' on Klipper master since the Generic Cartesian rework (May 2025). Asked
        before any move: a trigger the referee cannot read would creep the head past it."""
        if self.key is None:
            names = self.kl.request('query_endstops/status')
            self.key = next((key for key in ('stepper_' + self.axis, self.axis) if key in names), None)
            if self.key is None:
                raise SystemExit('Klipper reports no endstop for %s (it reports: %s), so the '
                                 'endstop referee cannot see the head reach it; nothing moved '
                                 'toward it' % (self.axis, ', '.join(names) or 'none'))
        return self.key

    def _triggered(self) -> bool:
        return self.kl.request('query_endstops/status').get(self._endstop_key()) == 'TRIGGERED'

    def _creep(self, lie: float, step: float, feed: int, travelled: float) -> 'float | None':
        """Step toward the endstop until it triggers; returns the travel at the trigger."""
        while travelled < CREEP_RANGE - 1.0:
            travelled += step
            self.kl.gcode('G1 %s%.3f F%d\nM400' % (self.axis.upper(),
                                                   lie + self.home_dir * travelled, feed))
            if self._triggered():
                return travelled
        return None

    def _measure(self) -> 'float | None':
        a = self.axis
        other = 'y' if a == 'x' else 'x'
        self._endstop_key()
        start = self.endstop - self.home_dir * CREEP_START
        lie = self.endstop - self.home_dir * CREEP_RANGE
        self.kl.gcode('G90\nG1 %s%.2f %s%.2f F6000\nM400'
                      % (a.upper(), start, other.upper(), self.park_other))
        # SET_HOMED=<axis>: the default marks Z homed as well
        self.kl.gcode('SET_KINEMATIC_POSITION %s=%.3f SET_HOMED=%s' % (a.upper(), lie, a.upper()))
        # fast coarse approach, then back off one coarse step and creep in fine steps
        coarse = self._creep(lie, COARSE_STEP, 3000, 0.0)
        if coarse is None:
            home_xy(self.kl, 'G28 %s' % a.upper())
            return None
        back = max(0.0, coarse - COARSE_STEP)
        self.kl.gcode('G1 %s%.3f F1800\nM400' % (a.upper(), lie + self.home_dir * back))
        travelled = self._creep(lie, CREEP_STEP, 1200, back)
        home_xy(self.kl, 'G28 %s' % a.upper())
        return None if travelled is None else CREEP_START - travelled

    def calibrate(self):
        offset = self._measure()
        if offset is None or abs(offset) > 1.5:
            raise SystemExit('endstop referee calibration failed on %s (offset %s) — '
                             'check the endstop before tuning current' % (self.axis, offset))
        self.bias = offset

    def slipped(self) -> 'float | None':
        """Bias-corrected head offset; None = out of range (a massive slip)."""
        offset = self._measure()
        return None if offset is None else offset - self.bias


AXIS_LIMIT_LIFTED = 1000000


def stroke_peak(span: float, vec: 'tuple[float, float]', accel: float) -> float:
    """The belt speed a stress stroke reaches: standstill to standstill over 2*span along
    vec, braking as hard as it accelerates (free_strokes)."""
    factor = math.hypot(*vec)                   # belt speed per unit of head feed
    return math.sqrt(2 * span * factor * accel) * factor


def stroke_accel(speed: float, span: float, vec: 'tuple[float, float]') -> int:
    """The accel, in hundreds, a stress stroke needs to reach this belt speed."""
    return int(math.ceil(speed ** 2 / (2 * span * math.hypot(*vec) ** 3) / 100.0)) * 100


def velocity_caps(kinematics: str, vec: 'tuple[float, float]', max_velocity: float,
                  limits: 'dict | None' = None) -> 'dict[str, tuple[float, float]]':
    """{option: (its value, the belt speed it lets a stroke along vec run)}. max_velocity
    caps the head and a belt runs |vec| times the head speed, except on Kalico's
    limited_corexy, where it caps the belt itself; limited_cartesian caps each axis too
    (max_x_velocity, max_y_velocity: the values in force, axis_limits)."""
    factor = math.hypot(*vec)
    caps = {'max_velocity': (max_velocity, max_velocity if kinematics == 'limited_corexy'
                             else max_velocity * factor)}
    for axis, part, value in zip('xy', vec, limits['velocities'] if limits else ()):
        if part:                    # a stroke there runs along its motor's axis: its belt
            caps['max_%s_velocity' % axis] = (value, value)
    return caps


def belt_cap(caps: 'dict[str, tuple[float, float]]') -> float:
    return min(cap for _, cap in caps.values())


def velocity_needs(caps: 'dict[str, tuple[float, float]]', speed: float) -> 'list[str]':
    """The velocity limits to raise, and to what, for a stroke to reach this belt speed."""
    return ['%s to %d' % (option, math.ceil(speed * value / cap))
            for option, (value, cap) in caps.items() if cap < speed]


def belt_top(span: float, vec: 'tuple[float, float]', accel: float, cap: float) -> float:
    """The fastest belt speed a stress stroke runs: its peak, or the belt_cap."""
    return min(stroke_peak(span, vec, accel), cap)


def accel_along(kinematics: str, vec: 'tuple[float, float]', accel: float,
                limits: 'dict | None' = None) -> float:
    """The acceleration a head move along vec gets for M204 S<accel>: Kalico's limited_*
    kinematics cap it by the per-axis limits in force (axis_limits) the way their
    check_move does, scaled by M204 with scale_xy_accel."""
    if not limits:
        return accel
    (ax, ay), (x, y) = limits['accels'], vec
    length = math.hypot(x, y)
    if kinematics == 'limited_corexy':
        cap = length / max(abs(x / ax + y / ay), abs(x / ax - y / ay))
        scale = max(ax, ay)
    else:
        cap = min(ax / max(abs(x) / length, sys.float_info.epsilon),
                  ay / max(abs(y) / length, sys.float_info.epsilon))
        scale = math.hypot(ax, ay)
    return min(accel, cap * accel / scale if limits['scale'] else cap)


def axis_limits(kl: Klippy, kinematics: str) -> 'dict | None':
    """The per-axis limits Kalico's limited_* kinematics hold now, set at runtime or not:
    SET_KINEMATICS_LIMIT without parameters reports them. None on other kinematics."""
    if not kinematics.startswith('limited_'):
        return None
    report = '\n'.join(kl.gcode_output('SET_KINEMATICS_LIMIT'))

    def values(name):
        found = re.search(r'max_%s: [(\[]([^)\]]*)' % name, report)
        return [float(value) for value in found.group(1).split(',')] if found else None
    accels, velocities = values('accels'), values('velocities')
    if not accels or (kinematics == 'limited_cartesian') != bool(velocities):
        raise SystemExit('SET_KINEMATICS_LIMIT did not report the %s limits (%r). Nothing was '
                         'moved' % (kinematics, report))
    return {'accels': accels[:2], 'velocities': (velocities or [])[:2],
            'scale': 'limits scale with' in report}


def accel_caps(limits: dict, lifted: str = '') -> str:
    """SET_KINEMATICS_LIMIT with the per-axis accel limits in force, those of the axes in
    `lifted` out of the way. SCALE=0 while lifted: with scale_xy_accel the other axis's
    limit would shrink against the lifted one."""
    return 'SET_KINEMATICS_LIMIT SCALE=%d %s' % (0 if lifted else limits['scale'], ' '.join(
        '%s_ACCEL=%s' % (axis.upper(), AXIS_LIMIT_LIFTED if axis in lifted else repr(value))
        for axis, value in zip('xy', limits['accels'])))


def keep_axis_limits(kl: Klippy, limits: 'dict | None', restores: list):
    """The per-axis accel limits in force come back after the run: registered before any lift."""
    if limits:
        restores.append(lambda: kl.gcode(accel_caps(limits)))


def lift_axis_limits(kl: Klippy, kinematics: str, limits: 'dict | None', motor: str):
    """Kalico's limited_* kinematics cap each axis's acceleration whatever M204 asks: a
    stroke would run below its rung, and a rung never run would 'hold'. Lifted for this
    motor's strokes: on limited_corexy both axes (the strokes run diagonals), on
    limited_cartesian its own, the other axis keeps its limit for the moves that set the
    strokes up. The velocity limits stay: rungs above them are not run (velocity_caps)."""
    if limits:
        kl.gcode(accel_caps(limits, 'xy' if coupled_xy(kinematics) else motor))


def live_limits(kl: Klippy) -> dict:
    """What the next moves obey, set at runtime or not: the toolhead's max_velocity and
    minimum_cruise_ratio, gcode_move's speed_factor (M220, 1.0 at 100%)."""
    status = kl.request('objects/query', {'objects': {
        'toolhead': ['max_velocity', 'minimum_cruise_ratio'],
        'gcode_move': ['speed_factor']}})['status']
    return dict(status['toolhead'], speed_factor=status['gcode_move']['speed_factor'])


def shaper_freqs(kl: Klippy) -> 'dict[str, str]':
    """The X/Y input shaper frequencies in force: SET_INPUT_SHAPER without parameters
    reports them ('shaper_type_x:mzv shaper_freq_x:40.000 ...'), no status object does."""
    return dict(re.findall(r'shaper_freq_([xy]):(\S+)', '\n'.join(kl.gcode_output('SET_INPUT_SHAPER'))))


def free_strokes(kl: Klippy, settings: dict, limits: dict, restores: list):
    """Let the strokes run as planned, each change's way back registered before the change
    (a Stop may land on any command):
    - minimum_cruise_ratio 0: Klipper brakes short moves early (0.5 by default), a 50 mm
      stroke at 3000 mm/s2 peaked at 274 mm/s and a 300 mm/s rung 'held' at that;
    - M220 S100: a speed factor left from a print scales every stroke;
    - no input shaping: it smooths what the motors get below the planned move, a
      stroke's peak and its accel alike (unshaped is the harder load)."""
    restores.append(lambda: kl.gcode('SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=%s'
                                     % limits['minimum_cruise_ratio']))
    kl.gcode('SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0')
    if limits['speed_factor'] != 1:
        restores.append(lambda: kl.gcode('M220 S%g' % (limits['speed_factor'] * 100)))
        kl.gcode('M220 S100')
    if 'input_shaper' in settings:
        freqs = shaper_freqs(kl)
        if any(float(freq) for freq in freqs.values()):
            restores.append(lambda: kl.gcode('SET_INPUT_SHAPER ' + ' '.join(
                'SHAPER_FREQ_%s=%s' % (axis.upper(), freq) for axis, freq in sorted(freqs.items()))))
            kl.gcode('SET_INPUT_SHAPER ' + ' '.join(
                'SHAPER_FREQ_%s=0' % axis.upper() for axis in sorted(freqs)))


def run_rung(kl: Klippy, board, motor: str, current: float, configured: float,
             vec: 'tuple[float, float]', span: float, accel: float, check=lambda: None):
    """One rung at `current`. check() runs before every stroke pair (about a second each,
    Klipper's own polling cadence): a whole rung is 15-30 s of load, a driver that
    warns of over-temperature may shut down within seconds (#133)."""
    cx, cy = board.center
    check()
    # from one end: the first stroke runs the full 2*span too
    home_xy(kl, 'G28 X Y\nG90\nM204 S%.0f\nG1 X%.1f Y%.1f F6000\nM400'
            % (accel, cx - span * vec[0], cy - span * vec[1]))
    # %r: the exact current (the bisection's 3 decimals, the config's 0.566), not 0.57
    kl.gcode('SET_TMC_CURRENT STEPPER=stepper_%s CURRENT=%r' % (motor, current))
    factor = math.hypot(*vec)                   # belt speed per unit of head feed
    for belt in BELT_SPEEDS:
        feed = belt / factor * 60
        for _ in range(STROKES_PER_SPEED):
            check()
            # a stroke reverses through zero speed anyway: pausing between pairs keeps the load
            kl.gcode('G1 X%.1f Y%.1f F%.0f\nG1 X%.1f Y%.1f F%.0f\nM400'
                     % (cx + span * vec[0], cy + span * vec[1], feed,
                        cx - span * vec[0], cy - span * vec[1], feed))
    kl.gcode('G1 X%.1f Y%.1f F6000\nM400' % (cx, cy))
    kl.gcode('SET_TMC_CURRENT STEPPER=stepper_%s CURRENT=%r' % (motor, configured))


def unify_recommendation(recommended: 'dict[str, float]', configured: 'dict[str, float]',
                         coupled: bool, per_motor: bool) -> 'dict[str, float]':
    """On coupled XY both motors get the MAX of the two recommendations: they share
    one kinematics, and their measured threshold gap is mechanical drag — which
    drifts with tension and wear (measured: a per-motor 0.60 A hit a speed ceiling
    its twin at 0.95 A did not). NEVER above each motor's own configured
    run_current though: the config is the motor's rating boundary, and the A/B
    motors are not guaranteed to be the same part. Cartesian axes keep per-motor
    values (the X and Y motors often ARE different parts)."""
    if per_motor or not coupled or len(recommended) < 2:
        return recommended
    top = max(recommended.values())
    return {m: min(top, configured[m]) for m in recommended}


def run_current_tune(args) -> int:
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        return current_tune(kl, args)
    finally:
        kl.close()


def current_tune(kl: Klippy, args) -> int:
    from .collect import motor_label
    motors = ['x', 'y'] if args.axis == 'xy' else [args.axis]
    settings = kl.settings()
    refuse_multi_motor(settings, ''.join(motors))
    hw = {m: detect_hardware(kl, m, accel=False) for m in motors}
    board = hw[motors[0]]
    configured = {m: float(settings['tmc%s stepper_%s' % (hw[m].driver.name, m)]['run_current'])
                  for m in motors}
    span = min(25.0, board.axis_span / 8)
    limits = live_limits(kl)
    per_axis = axis_limits(kl, board.kinematics)
    accels = {}
    for m in motors:
        vec = stress_vector(board.kinematics, m)
        top = BELT_SPEEDS[-1]
        caps = velocity_caps(board.kinematics, vec, limits['max_velocity'], per_axis)
        if belt_cap(caps) < top:
            # the action first: the display shows its first characters (failure_display)
            raise SystemExit('raise %s or more: now %s, it caps motor %s at %.0f of the %d mm/s '
                             'the pattern needs. Nothing was moved'
                             % (' and '.join(velocity_needs(caps, top)),
                                ', '.join('%g' % value for value, cap in caps.values() if cap < top),
                                motor_label(m), belt_cap(caps), top))
        # the printer's own load by default: what its per-axis limits give this stroke on
        # Kalico's limited_* kinematics; an ACCEL given runs as asked (lift_axis_limits)
        accels[m] = args.accel or accel_along(board.kinematics, vec, board.max_accel, per_axis)
        if stroke_peak(span, vec, accels[m]) < top:
            raise SystemExit('raise ACCEL to %d or more: at %.0f a stroke of motor %s peaks at %.0f '
                             'of the %d mm/s the pattern needs. Nothing was moved'
                             % (stroke_accel(top, span, vec), accels[m], motor_label(m),
                                stroke_peak(span, vec, accels[m]), top))

    print('Current tuning on motor(s) %s: worst-case pattern (single-motor load, belts %s mm/s, '
          '±%.0f mm), endstop referee, margin %.1fx over the measured skip threshold'
          % ('+'.join(motor_label(m) for m in motors), '/'.join(map(str, BELT_SPEEDS)), span,
             args.margin))
    for m in motors:
        capped = (' (what the per-axis limits give it of max_accel %g)' % board.max_accel
                  if not args.accel and accels[m] < board.max_accel else '')
        print('  motor %s: configured run_current %.2f A, strokes at %.0f mm/s2%s'
              % (motor_label(m), configured[m], accels[m], capped))
    if args.dry_run:
        return 0
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1

    refuse_if_printing(kl)
    screen = Screen(kl, board.display)
    guard = ThermalGuard(kl, settings)
    recommended, thresholds = {}, {}
    refuse_blind_z_hop(kl, settings)
    guard.preflight()
    restores = []
    try:
        home_xy(kl, 'G28 X Y\nG90')
        free_strokes(kl, settings, limits, restores)
        keep_axis_limits(kl, per_axis, restores)
        for m in motors:
            label = motor_label(m)
            lift_axis_limits(kl, board.kinematics, per_axis, m)
            ref = Referee(kl, referee_axis(board.kinematics, m), settings,
                          board.center[0 if referee_axis(board.kinematics, m) == 'y' else 1])
            guard.check()
            ref.calibrate()
            vec = stress_vector(board.kinematics, m)
            rungs = []

            def holds(current, m=m, vec=vec, ref=ref, label=label):
                screen.update('Chopper current %s @ %.2fA' % (label, current), force=True,
                              short='%s test %.2fA' % (label, current))
                run_rung(kl, board, m, current, configured[m], vec, span, accels[m], guard.check)
                guard.check()                        # before the referee's crawl
                slip = ref.slipped()
                held = slip is not None and abs(slip) < SLIP_HEAD_MM
                rungs.append((current, held))
                print('  motor %s @ %.2f A: %s'
                      % (label, current, 'holds' if held
                         else 'SLIP %s' % ('out of range' if slip is None else '%+.2f mm' % slip)))
                return held

            print('\n=== Motor %s ===' % label)
            if not holds(configured[m]):
                raise SystemExit('motor %s skips at its CONFIGURED current on the worst-case '
                                 'pattern — fix that before tuning current' % label)
            if holds(args.min_current):
                threshold = args.min_current
                print('  holds even at the search floor %.2f A' % threshold)
            else:
                threshold = bisect_threshold(holds, args.min_current, configured[m],
                                             args.resolution)
            thresholds[m] = threshold
            recommended[m] = min(configured[m], round(threshold * args.margin, 2))
            print('  skip threshold ~%.2f A -> recommended run_current %.2f A (%.1fx margin)'
                  % (threshold, recommended[m], args.margin))
            screen.update('Chopper: %s current %.2fA' % (label, recommended[m]), force=True,
                          short='%s run %.2fA' % (label, recommended[m]))
    finally:
        run_restore(
            *[lambda m=m: kl.gcode('SET_TMC_CURRENT STEPPER=stepper_%s CURRENT=%r'
                                   % (m, configured[m])) for m in motors],
            lambda: kl.gcode('M204 S%.0f' % board.max_accel),
            *restores,
            lambda: rehome_unless_hot(kl))

    unified = unify_recommendation(recommended, configured, coupled_xy(board.kinematics),
                                   args.per_motor)
    if unified != recommended:
        print('\nCoupled-XY drive: both motors get the max of the recommendations '
              '(%.2f A, capped by each motor\'s configured current) — shared '
              'kinematics, and the threshold gap is mechanical drag that drifts. '
              'PER_MOTOR=1 keeps the measured split.' % max(unified.values()))
        recommended = unified
    save_json(STATE, {motor_label(m): {'threshold': thresholds[m], 'recommended': recommended[m],
                                       'margin': args.margin} for m in motors},
              merge=True)                              # the panel's Results shows these
    screen.final('Current: ' + ' \u00b7 '.join(
        '%s skip %.2fA -> run %.2fA' % (motor_label(m), thresholds[m], recommended[m])
        for m in motors), ' '.join('%s %.2fA' % (motor_label(m), recommended[m]) for m in motors))
    print('\n=== Summary ===')
    for m in motors:
        print('[tmc%s stepper_%s]\nrun_current: %.2f' % (hw[m].driver.name, m, recommended[m]))
    if args.save:
        from .analyze import run_save_currents
        from .moonraker import Moonraker
        items = [(hw[m].driver.name, 'stepper_' + m, recommended[m]) for m in motors
                 if recommended[m] != configured[m]]
        if items:
            run_save_currents(Moonraker(args.url), items)
            print('Re-run CHOPPER_TUNE now: the chopper optimum depends on the run current')
        else:
            print('Nothing to save: recommended current equals the configured one')
    else:
        print('Re-run with SAVE=1 to persist, then CHOPPER_TUNE at the new current')
    return 0
