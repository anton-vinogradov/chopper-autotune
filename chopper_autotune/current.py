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

from .collect import Screen, coupled_xy, detect_hardware, refuse_if_printing, run_restore
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

    def _triggered(self) -> bool:
        return self.kl.request('query_endstops/status').get('stepper_' + self.axis) == 'TRIGGERED'

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
        start = self.endstop - self.home_dir * CREEP_START
        lie = self.endstop - self.home_dir * CREEP_RANGE
        self.kl.gcode('G90\nG1 %s%.2f %s%.2f F6000\nM400'
                      % (a.upper(), start, other.upper(), self.park_other))
        self.kl.gcode('SET_KINEMATIC_POSITION %s=%.3f' % (a.upper(), lie))
        # fast coarse approach, then back off one coarse step and creep in fine steps
        coarse = self._creep(lie, COARSE_STEP, 3000, 0.0)
        if coarse is None:
            self.kl.gcode('G28 %s' % a.upper())
            return None
        back = max(0.0, coarse - COARSE_STEP)
        self.kl.gcode('G1 %s%.3f F1800\nM400' % (a.upper(), lie + self.home_dir * back))
        travelled = self._creep(lie, CREEP_STEP, 1200, back)
        self.kl.gcode('G28 %s' % a.upper())
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


def run_rung(kl: Klippy, board, motor: str, current: float, configured: float,
             vec: 'tuple[float, float]', span: float, accel: float):
    cx, cy = board.center
    kl.gcode('G28 X Y\nG90\nM204 S%.0f\nG1 X%.1f Y%.1f F6000\nM400' % (accel, cx, cy))
    kl.gcode('SET_TMC_CURRENT STEPPER=stepper_%s CURRENT=%.2f' % (motor, current))
    factor = math.hypot(*vec)                   # belt speed per unit of head feed
    moves = []
    for belt in BELT_SPEEDS:
        feed = belt / factor * 60
        for _ in range(STROKES_PER_SPEED):
            moves += ['G1 X%.1f Y%.1f F%.0f' % (cx + span * vec[0], cy + span * vec[1], feed),
                      'G1 X%.1f Y%.1f F%.0f' % (cx - span * vec[0], cy - span * vec[1], feed)]
    moves.append('G1 X%.1f Y%.1f F6000' % (cx, cy))
    kl.gcode('\n'.join(moves) + '\nM400')
    kl.gcode('SET_TMC_CURRENT STEPPER=stepper_%s CURRENT=%.2f' % (motor, configured))


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
    hw = {m: detect_hardware(kl, m) for m in motors}
    board = hw[motors[0]]
    settings = kl.settings()
    configured = {m: float(settings['tmc%s stepper_%s' % (hw[m].driver.name, m)]['run_current'])
                  for m in motors}
    accel = args.accel or board.max_accel
    span = min(25.0, board.axis_span / 8)

    print('Current tuning on motor(s) %s: worst-case pattern (single-motor load, belts %s mm/s, '
          'accel %.0f, ±%.0f mm), endstop referee, margin %.1fx over the measured skip threshold'
          % ('+'.join(motor_label(m) for m in motors), '/'.join(map(str, BELT_SPEEDS)),
             accel, span, args.margin))
    for m in motors:
        print('  motor %s: configured run_current %.2f A' % (motor_label(m), configured[m]))
    if args.dry_run:
        return 0
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1

    refuse_if_printing(kl)
    screen = Screen(kl, board.display)
    recommended, thresholds = {}, {}
    try:
        kl.gcode('G28 X Y\nG90')
        for m in motors:
            label = motor_label(m)
            ref = Referee(kl, referee_axis(board.kinematics, m), settings,
                          board.center[0 if referee_axis(board.kinematics, m) == 'y' else 1])
            ref.calibrate()
            vec = stress_vector(board.kinematics, m)
            rungs = []

            def holds(current, m=m, vec=vec, ref=ref, label=label):
                screen.update('Chopper current %s @ %.2fA' % (label, current), force=True)
                run_rung(kl, board, m, current, configured[m], vec, span, accel)
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
            screen.update('Chopper: %s current %.2fA' % (label, recommended[m]), force=True)
    finally:
        run_restore(
            *[lambda m=m: kl.gcode('SET_TMC_CURRENT STEPPER=stepper_%s CURRENT=%.2f'
                                   % (m, configured[m])) for m in motors],
            lambda: kl.gcode('M204 S%.0f\nG28 X Y' % board.max_accel))

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
        for m in motors))
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
