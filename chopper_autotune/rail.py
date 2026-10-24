"""A rail: the motors Klipper steps together on one axis, stepper_x and its twins
stepper_x1... (AWD, a two-motor gantry, #129). COLLECT, FIND_SPEED, TUNE and DEMO run a
rail whole: every driver gets the same registers (set_rail_fields), and the rail moves by a
G1 of the head along the vector that turns its belt alone, with every X/Y motor on from the
first homing to the last. MAP and CURRENT drive one motor: they refuse such an axis
(refuse_multi_motor)."""
from __future__ import annotations

import math
import sys

from . import tmc
from .collect import (ForceMove, Hardware, RunStopped, ThermalGuard, autotune_goal,
                      driver_of, exit_spreadcycle, gear_factor, home_xy, motor_label, rail_steppers,
                      rail_twins, refuse_after_shutdown, refuse_blind_z_hop, refused,
                      rehome_unless_hot, release_gantry, restore_chopper, run_restore,
                      set_rail_fields, travel_for, xy_driver_sections)
from .current import (accel_along, axis_limits, belt_cap, free_strokes, live_limits, stress_vector,
                      velocity_caps, velocity_needs)
from .klippy import Klippy, KlippyError

KINEMATICS = ('cartesian', 'limited_cartesian', 'corexy', 'limited_corexy')
EDGE_MM = 25.0              # kept between a move and each bed edge, or EDGE_SHARE of the axis
EDGE_SHARE = 0.1
MIN_Z_MM = 5.0
APPROACH_FEED = 6000
FAILED_IN_A_ROW = 4         # attempts: then the run stops instead of re-homing before each next
STEP_OPTIONS = ('rotation_distance', 'gear_ratio', 'full_steps_per_rotation', 'microsteps')
# what runs a driver otherwise than its twin under the same chopper registers: named
# before the run, not refused (the current, the mode switch, CoolStep, the high-velocity
# switch, interpolation)
RUNNING_OPTIONS = ('run_current', 'hold_current', 'stealthchop_threshold', 'coolstep_threshold',
                   'driver_semin', 'driver_semax', 'driver_seup', 'driver_sedn', 'driver_seimin',
                   'driver_sfilt', 'high_velocity_threshold', 'driver_vhighfs', 'driver_vhighchm',
                   'interpolate')


class GantryUnhomed(RunStopped):
    """A move needs an axis homed that is not, and the run stops and hands the gantry over
    (decision 7): X or Y lost its homing mid-run (M84, a failed G28), where the motors of a
    rail may have settled apart while off and a [motors_sync] lost its sync; or Z, which a
    mesh or [z_thermal_adjust] moves under the moves."""


class RegistersStuck(RunStopped):
    """A register or mode write around a homing failed: no G28 on registers unknown."""


def step_option(settings: dict, stepper: str, option: str):
    """How the stepper's section sets this part of its step distance, as one number."""
    section = settings.get(stepper) or {}
    if option == 'gear_ratio':
        return gear_factor(section.get('gear_ratio'))
    default = 200 if option == 'full_steps_per_rotation' else None
    value = section.get(option, default)
    return None if value is None else float(value)


def refuse_unsupported(settings: dict, axis: str):
    """A rail runs only where one register set and one G1 move all its motors alike:
    checked from the config before anything moves, the dry run too. Each refusal starts
    with what to do or what fails (failure_display)."""
    rail = rail_steppers(settings, axis)
    names, motor = ', '.join(rail), motor_label(axis)
    kinematics = (settings.get('printer') or {}).get('kinematics', '')
    if kinematics not in KINEMATICS:
        raise SystemExit('not on %s with two motors on an axis (motor %s: %s): cartesian and CoreXY '
                         'only. Nothing was moved' % (kinematics, motor, names))
    if 'dual_carriage' in settings:
        raise SystemExit('not with [dual_carriage] and two motors on an axis (motor %s: %s): a G1 '
                         'moves the active carriage alone. Nothing was moved' % (motor, names))
    models = {stepper: driver_of(settings, stepper) for stepper in rail}
    missing = [stepper for stepper, model in models.items() if model is None]
    if missing:
        raise SystemExit('%s: no TMC section the tool can set (%s), and every driver of motor %s '
                         'gets the registers. Nothing was moved'
                         % (', '.join(missing), ', '.join('tmc' + name for name in sorted(tmc.DRIVERS)),
                            motor))
    if len(set(models.values())) > 1:
        raise SystemExit('use one driver model on motor %s (%s): one register set runs another '
                         'chopper on each. Nothing was moved'
                         % (motor, ', '.join('%s TMC%s' % item for item in models.items())))
    managed = [stepper for stepper in rail if autotune_goal(settings, stepper) is not None]
    if managed and len(managed) < len(rail):
        raise SystemExit('put klipper_tmc_autotune on all of %s or on none: it manages %s only, and '
                         'the motor would end on two choppers. Nothing was moved'
                         % (names, ', '.join(managed)))
    for option in STEP_OPTIONS:
        values = {stepper: step_option(settings, stepper, option) for stepper in rail}
        if len(set(values.values())) > 1:
            raise SystemExit('match %s on motor %s (%s): motors of one axis that step differently '
                             'pull apart on every move. Nothing was moved'
                             % (option, motor, ', '.join(
                                 '%s %s' % (stepper, 'unset' if value is None else '%g' % value)
                                 for stepper, value in values.items())))


def tilted(settings: dict) -> bool:
    """[bed_tilt] tilts the bed by config: every G1 of X or Y moves Z along (its move
    transform), and with Z unhomed fails ('Must home axis first')."""
    tilt = settings.get('bed_tilt') or {}
    return any(float(tilt.get(option) or 0) for option in ('x_adjust', 'y_adjust'))


def refuse_rail_run(kl: Klippy, csv: bool):
    """What a rail run needs of this run and of the printer now, before anything moves:
    the stream, and room under the nozzle (a rail moves the head across most of the
    bed at the height it finds), a homed Z under a tilted bed."""
    if csv:
        raise SystemExit('drop CSV=1: runs on a two-motor axis need the stream, the default. '
                         'Nothing was moved')
    toolhead = kl.request('objects/query', {'objects': {'toolhead': ['homed_axes', 'position']}})[
        'status']['toolhead']
    if 'z' in toolhead['homed_axes'] and toolhead['position'][2] < MIN_Z_MM:
        raise SystemExit('raise Z to %g mm or more (G1 Z10), then retry: a two-motor axis moves the '
                         'head across most of the bed, now at Z %.1f. Nothing was moved'
                         % (MIN_Z_MM, toolhead['position'][2]))
    if 'z' not in toolhead['homed_axes'] and tilted(kl.settings()):
        raise SystemExit('home all axes (G28), then retry: [bed_tilt] moves Z on every move of a '
                         'two-motor axis, and Z is not homed. Nothing was moved')


def config_limits(settings: dict, kinematics: str) -> 'dict | None':
    """Kalico's per-axis limits of limited_* as the config sets them, in axis_limits' shape:
    a dry run sends no G-code (SET_KINEMATICS_LIMIT reports the ones in force)."""
    if not kinematics.startswith('limited_'):
        return None
    printer = settings['printer']
    accels = [float(printer.get('max_%s_accel' % axis, printer['max_accel'])) for axis in 'xy']
    top = math.sqrt(2) * float(printer['max_velocity'])
    velocities = [float(printer.get('max_%s_velocity' % axis, top))
                  for axis in 'xy'] if kinematics == 'limited_cartesian' else []
    return {'accels': accels, 'velocities': velocities, 'scale': bool(printer.get('scale_xy_accel'))}


def config_registers(drive: Hardware) -> str:
    """The chopper registers the config gives the driver: its driver_* lines, Klipper's own
    for the rest."""
    return tmc.Chopper(**dict(drive.driver.default.fields(), **drive.baseline)).label()


def refuse_cut(caps: dict, speed: float, what: str):
    """Refuse a speed G1 would cut: max_velocity caps a move without an error, and the
    window would measure a slower belt than the run asked (caps: velocity_caps)."""
    cap = belt_cap(caps)
    if cap < speed:
        # the action first: the display shows its first characters (failure_display)
        raise SystemExit('raise %s or more: now %s, it caps %s at %.0f of the %d mm/s the run '
                         'needs. Nothing was moved'
                         % (' and '.join(velocity_needs(caps, speed)),
                            ', '.join('%g' % value for value, limit in caps.values() if limit < speed),
                            what, cap, speed))


def rails_first(settings: dict, axes: 'list[str]', run: str) -> 'list[str]':
    """The axes of a run of both motors, a rail before a motor alone on its axis: that one
    runs with every X/Y motor off (FORCE_MOVE), and before the rail's run its motors would
    come back on apart, unsynced. Said where both kinds meet."""
    rails = [axis for axis in axes if rail_twins(settings, axis)]
    if not rails or len(rails) == len(axes):
        return axes
    ordered = sorted(axes, key=lambda axis: axis not in rails)
    alone = next(axis for axis in ordered if axis not in rails)
    print('note: motor %s goes first, because motor %s runs alone on its axis, with every X/Y '
          'motor off%s'
          % (motor_label(rails[0]), motor_label(alone), '. Run G28 and SYNC_MOTORS again after the '
             '%s' % run if 'motors_sync' in settings else ''))
    return ordered


def resync_first(settings: dict, axes: 'list[str]', axis: str) -> str:
    """What a later run of the rail on `axis` needs first, after a run of `axes` (in
    rails_first's order) went on from it to a motor alone: that one ran with every X/Y
    motor off, and a [motors_sync] lost its sync; '' otherwise."""
    alone = [later for later in axes[axes.index(axis) + 1:] if not rail_twins(settings, later)]
    if not alone or not rail_twins(settings, axis) or 'motors_sync' not in settings:
        return ''
    return ('Motor %s then ran alone, with every X/Y motor off, so the motors of %s lost their '
            'sync. Run G28 and SYNC_MOTORS first.' % (motor_label(alone[0]), motor_label(axis)))


def motion_for(kl: Klippy, hw: Hardware, args):
    """How the run moves the motor: a rail when it has twins (rail_of), else FORCE_MOVE."""
    return RailMove(kl, hw, args) if hw.twins else ForceMove(kl, hw)


class RailMove:
    """How a run moves a rail: one G1 of the head along stress_vector (the axis on
    cartesian; a diagonal on CoreXY, where the other rail's belt holds still), through the
    center of the bed and back. Speeds, accels and travel stay belt mm, as a FORCE_MOVE's:
    the head gets F = v/k*60 and M204 S = a/k, k the belt mm per head mm. Klipper keeps
    the position, so no drift to account and no homing on a schedule: a homing can leave
    the two motors of a belt apart (#164). The X/Y motors stay on the whole run, and the
    re-home after a failed attempt runs on the registers the drivers had before the run,
    never on a candidate's: a sensorless homing was tuned on those. The show of both
    motors moves several rails (`also`) by one G1 along the head's course `vec`, in head mm
    (k = 1), each rail on its own registers."""

    def __init__(self, kl: Klippy, hw: Hardware, args, also: 'tuple[Hardware, ...]' = (),
                 vec: 'tuple[float, float] | None' = None):
        self.kl, self.hw = kl, hw
        self.rails = [hw, *also]
        self.settings = settings = kl.settings()
        refuse_rail_run(kl, args.csv)
        self.vec = vec or stress_vector(hw.kinematics, hw.stepper.rsplit('_', 1)[-1])
        self.k = math.hypot(*self.vec)
        self.unit = tuple(part / self.k for part in self.vec)
        self.bed = [(float(settings['stepper_' + axis].get('position_min', 0.0)),
                     float(settings['stepper_' + axis]['position_max'])) for axis in 'xy']
        self.edges = [max(EDGE_MM, EDGE_SHARE * (hi - lo)) for lo, hi in self.bed]
        # each axis the move runs along keeps its edge: the head goes L/(2k)*|unit| each way
        self.limit = min(2 * self.k * ((hi - lo) / 2 - edge) / abs(part)
                         for (lo, hi), edge, part in zip(self.bed, self.edges, self.unit) if part)
        status = kl.request('objects/query', {'objects': {
            'toolhead': ['homed_axes'], 'bed_mesh': ['profile_name', 'profiles']}})['status']
        self.homed = status['toolhead']['homed_axes']
        self.origin = self.offset()
        self.mesh = status.get('bed_mesh') or {}
        self.limits = live_limits(kl)
        self.per_axis, self.per_axis_source = ((config_limits(settings, hw.kinematics), 'by config')
                                               if args.dry_run else
                                               (axis_limits(kl, hw.kinematics), 'now'))
        self.caps = self.belt_caps(hw.stepper.rsplit('_', 1)[-1])
        self.cap = belt_cap(self.caps)
        self.speed_cap = int(math.floor(self.cap + 1e-6))
        self.asked = hw.max_accel / 10
        self.guard = None
        self.stumbled = False
        self.failures = 0
        self.restores, self.reloads, self.freed = [], [], []

    @property
    def drives(self) -> 'list[Hardware]':
        return [drive for rail in self.rails for drive in rail.rail]

    @property
    def motors(self) -> str:
        """'motor A', or 'motors A and B' for a move of both."""
        return ('motor ' if len(self.rails) == 1 else 'motors ') + ' and '.join(
            rail.motor for rail in self.rails)

    def belt_caps(self, axis: str) -> dict:
        """velocity_caps of the belt of `axis`: along the vector that turns it alone, the
        per-axis limits in force counted."""
        return velocity_caps(self.hw.kinematics, stress_vector(self.hw.kinematics, axis),
                             self.limits['max_velocity'], self.per_axis)

    @property
    def resync(self) -> str:
        """The tail of a stop that switched the motors off: [motors_sync] lost its sync."""
        return '; run SYNC_MOTORS after you home again' if 'motors_sync' in self.settings else ''

    @property
    def standstill(self) -> tuple:
        """The noise floor runs with the motors holding: the guard checks it too."""
        return (self.guard.check,)

    def offset(self) -> 'list[float]':
        """The G-code offset in X and Y (homing_origin): Klipper adds it to every G1 after a
        homing (gcode_move base_position)."""
        return self.kl.request('objects/query', {'objects': {'gcode_move': ['homing_origin']}})[
            'status']['gcode_move']['homing_origin'][:2]

    def reach(self, asked: float) -> float:
        """The belt accel a move gets for M204 S<asked/k>: Kalico's limited_* may cap it."""
        return self.k * accel_along(self.hw.kinematics, self.vec, asked / self.k, self.per_axis)

    def per_axis_cut(self) -> str:
        """What the moves get of the accel asked, said where Kalico's per-axis limits cut it."""
        reached = self.reach(self.asked)
        return ('; per-axis limits (%s) give it %.0f' % (self.per_axis_source, reached)
                if reached < self.asked - 1e-6 else '')

    def accel(self, asked: 'float | None', top: float, cruise: float) -> float:
        """The run's belt accel as the moves get it. ACCEL= when given; else the tools'
        max_accel/10 when the top speed's move fits the travel with the cruise asked, the
        least multiple of 100 that fits up to the belt's own ceiling (max_accel of the
        head along the vector, k*max_accel), or that ceiling, where the cruise shrinks."""
        if not asked:
            ceiling = self.k * self.hw.max_accel

            def fits(value):
                return travel_for(top, self.reach(value), cruise) <= self.limit
            asked = self.hw.max_accel / 10
            if not fits(asked):
                asked = next((value for value in range(100, int(ceiling) + 1, 100) if fits(value)),
                             ceiling)
        self.asked = asked
        return self.reach(asked)

    def ends(self, travel: float, direction: int) -> 'list[list[float]]':
        """Where a move of `travel` belt mm starts and ends, in G-code coordinates: across
        the center of the bed, forth for direction 1, back for -1."""
        half = travel / (2 * self.k)
        return [[center + sign * half * part - origin
                 for center, part, origin in zip(self.hw.center, self.unit, self.origin)]
                for sign in (-direction, direction)]

    def margin(self, travel: float) -> float:
        """How near a move of `travel` comes to a bed edge."""
        half = travel / (2 * self.k)
        return min((hi - lo) / 2 - half * abs(part)
                   for (lo, hi), part in zip(self.bed, self.unit) if part)

    def course(self) -> str:
        if self.k > 1:
            other = motor_label('y' if self.hw.stepper.endswith('_x') else 'x')
            return 'on the X%sY diagonal (motor %s holds still)' % ('+' if self.vec[1] > 0 else '-',
                                                                     other)
        return 'along %s' % ('X' if self.vec[0] else 'Y')

    def plan(self, moves: 'list[tuple[float, float]]', extension: 'int | None' = None):
        """Say what the run does, before anything moves (the dry run prints the same), and
        refuse a speed G1 would cut (refuse_cut)."""
        hw = self.hw
        speeds = [speed for speed, _ in moves]
        refuse_cut(self.caps, max(speeds), 'motor ' + hw.motor)
        print('Motor %s is a rail of %d TMC%s drivers (%s): one G1 of the head moves them together %s; '
              'speeds and accels in belt mm, as on one motor'
              % (hw.motor, len(hw.rail), hw.driver.name, ', '.join(drive.stepper for drive in hw.rail),
                 self.course()))
        low, high = min(speeds), max(speeds)
        print('  head: F%s for %s mm/s of belt, M204 S%.0f for %.0f mm/s2 of belt%s'
              % ('%.0f' % (low / self.k * 60) + ('-%.0f' % (high / self.k * 60) if high > low else ''),
                 '%g' % low + ('-%g' % high if high > low else ''), self.asked / self.k, self.asked,
                 self.per_axis_cut()))
        print('  max_velocity %g caps the belt at %.0f mm/s' % (self.limits['max_velocity'], self.cap))
        if extension:
            print('  a curve still rising at the top extends the scan up to %d mm/s' % extension)
        self.conditions(*max(moves, key=lambda move: move[1]))

    def conditions(self, speed: float, travel: float):
        """What every move of the rails meets, said before anything moves: where the longest
        one (`travel` at `speed`) runs, what the run frees for its moves, each driver, how
        the drivers of a rail differ, the notes."""
        settings = self.settings
        start, end = self.ends(travel, 1)
        print('  moves through the bed center, the longest from X%.1f Y%.1f to X%.1f Y%.1f (G-code '
              'coordinates); nearest a bed edge %.1f mm, at %s mm/s (kept: X %g, Y %g mm)'
              % (start[0], start[1], end[0], end[1], self.margin(travel), '%g' % round(speed, 1),
                 *self.edges))
        freed = ['minimum_cruise_ratio %g -> 0' % self.limits['minimum_cruise_ratio']]
        if self.limits['speed_factor'] != 1:
            freed.append('speed factor %g%% -> 100%%' % (self.limits['speed_factor'] * 100))
        shaper = settings.get('input_shaper') or {}
        freqs = {axis: float(shaper.get('shaper_freq_' + axis) or 0) for axis in 'xy'}
        if any(freqs.values()):
            freed.append('input shaper (by config %s) -> off' % ', '.join(
                '%s %g Hz' % (axis.upper(), freq) for axis, freq in freqs.items()))
        print('  for the moves: %s (every homing runs on your own settings)' % ', '.join(freed))
        for drive in self.drives:
            print('  %s' % self.describe(drive))
        for rail in self.rails:
            differ = self.differences(rail)
            if differ:
                print('WARNING: the drivers of motor %s differ in %s: one register set may run them '
                      'unlike' % (rail.motor, '; '.join(differ)))
            homing = {drive.stepper: settings['tmc%s %s' % (drive.driver.name, drive.stepper)]
                      .get('home_current') for drive in rail.rail}
            if len(set(map(str, homing.values()))) > 1:
                print('WARNING: the drivers of motor %s home at different currents (Kalico '
                      'home_current: %s). A change of current can move a stepper, so a G28 can '
                      'leave %s apart. The run homes at its start and after a failed move. If a '
                      'mid-run homing leaves them apart, the run compares register sets measured '
                      'before and after it'
                      % (rail.motor, ', '.join('%s %s' % (stepper, '%g A' % float(current) if current
                                                          else 'its run_current')
                                               for stepper, current in homing.items()),
                         ' and '.join(homing)))
        self.notes()

    def describe(self, drive: Hardware) -> str:
        """One driver as the run finds it in the config."""
        from .analyze import shares_enable
        section = self.settings['tmc%s %s' % (drive.driver.name, drive.stepper)]
        if drive.autotune is not None:
            chopper = ('klipper_tmc_autotune (%s) sets registers and mode, read at the start'
                       % drive.autotune)
        else:
            chopper = 'registers %s, %s' % (config_registers(drive), 'stealthChop, spreadCycle '
                                            'for the run' if drive.stealth else 'spreadCycle')
        return '%s: %s; run_current %g A; %s' % (
            drive.stepper, chopper, float(section.get('run_current') or 0),
            'shares its enable pin (a re-enable puts the config toff back)'
            if shares_enable(self.settings, drive.stepper) else 'its own enable pin')

    def differences(self, rail: Hardware) -> 'list[str]':
        """What the drivers of the rail run unlike beside the registers the run sets."""
        sections = {drive.stepper: self.settings['tmc%s %s' % (drive.driver.name, drive.stepper)]
                    for drive in rail.rail}
        found = []
        registers = {drive.stepper: config_registers(drive)
                     for drive in rail.rail if drive.autotune is None}
        for name, values in [('registers', registers)] + [
                (option, {stepper: section.get(option) for stepper, section in sections.items()})
                for option in RUNNING_OPTIONS]:
            if len(set(map(str, values.values()))) > 1:
                found.append('%s (%s)' % (name, ', '.join('%s %s' % item for item in values.items())))
        return found

    def notes(self):
        settings = self.settings
        if 'z' not in self.homed:
            print('note: Z is not homed: the head crosses most of the bed at the height it is at; '
                  'clear clips, a brush or a purge bucket from its way, or home Z and raise it')
        name = self.mesh.get('profile_name')
        if name:
            print('note: the bed mesh %s is cleared for the run%s' % (
                name, ' and loaded back after it' if name in (self.mesh.get('profiles') or {})
                else '; no saved profile, the next print makes its own'))
        if 'skew_correction' in settings:
            print('note: [skew_correction] stays on: a loaded profile moves the other axis a little '
                  'along')
        if 'z_thermal_adjust' in settings:
            print('note: [z_thermal_adjust] stays on: it moves Z during the moves')
        if tilted(settings):
            print('note: [bed_tilt] stays on: it moves Z along every move')
        if 'motors_sync' in settings:
            print('note: [motors_sync]: run G28 and SYNC_MOTORS before the run; the run keeps the '
                  'motors on. A stop on heat switches them off with SET_STEPPER_ENABLE, and '
                  'motors_sync does not see it (its applied flag stays true): run SYNC_MOTORS '
                  'by hand after such a stop')
        holding, held = [], []
        for name in xy_driver_sections(settings):
            # Klipper records hold_current's default, the driver's maximum: the full run current
            hold = float(settings[name].get('hold_current') or float('inf'))
            run = float(settings[name].get('run_current') or 0)
            stepper = name.split(' ', 1)[1]
            holding.append('%s %s' % (stepper, '%g A' % hold if hold < run
                                      else 'its run_current %g A' % run))
            if hold >= run:
                held.append(stepper)
        print('Every X/Y motor holds under current the whole run: %s' % ', '.join(holding))
        if held:
            print('WARNING: %s stay at their full run_current the whole run, the waiting ones '
                  'too, so a waiting driver heats like a moving one: cool the drivers (#133). '
                  'Klipper advises against a hold_current below run_current: a change of '
                  'current can move a motor' % ', '.join(held))

    def manifest_fields(self) -> dict:
        return {'motion': 'rail', 'steppers': [drive.stepper for drive in self.hw.rail],
                'vector': list(self.vec), 'head_accel': round(self.asked / self.k, 3),
                'edges': self.edges, 'noise_floor': 'motors holding'}

    def prepare(self):
        """Home XY and go to the center, then free the moves of what would cut them
        (free_strokes, a print's M204; the bed mesh before the homing: the move to the
        center needs Z with it), each way back registered before its change. A driver
        still hot from an earlier stop ends the run first. The motors stay on from here to
        the end of the run."""
        kl, settings = self.kl, self.settings
        print('Preparing: home XY and go to the center; every X/Y motor stays on until the run ends')
        self.guard = ThermalGuard(kl, settings)
        refuse_blind_z_hop(kl, settings)        # before any motion or motor enable
        self.guard.preflight()
        try:
            self.limits = limits = live_limits(kl)
            self.restores.append(lambda: kl.gcode('M204 S%s' % limits['max_accel']))
            self.clear_mesh()
            self.home()
            freed = len(self.restores)
            free_strokes(kl, settings, limits, self.restores)
            self.freed = self.restores[freed:]
        except BaseException:
            run_restore(*self.restores, *self.reloads)
            raise

    def clear_mesh(self):
        """A loaded mesh moves Z under every G1: with Z unhomed each move fails, and the Z
        motors would sound in the window. Loaded back after the closing homing; an
        adaptive mesh, no saved profile, is not (the next print makes its own)."""
        kl = self.kl
        mesh = kl.request('objects/query', {'objects': {'bed_mesh': ['profile_name', 'profiles']}})[
            'status'].get('bed_mesh') or {}
        name = mesh.get('profile_name')
        if not name:
            return
        if name in (mesh.get('profiles') or {}):
            self.reloads.append(lambda: kl.gcode('BED_MESH_PROFILE LOAD="%s"' % name))
        else:
            print('note: the bed mesh %s is no saved profile: cleared for the run, not loaded back'
                  % name)
        kl.gcode('BED_MESH_CLEAR')

    def home(self):
        """G28 on the accel the run found, as the closing one: a homing takes the
        toolhead's, and a sensorless one was tuned on that, not on the run's M204; then
        the run's to the center. The run's freed limits are not in force here (prepare,
        rehome): a sensorless homing with the input shaper off stopped early on the
        second touch (#164)."""
        center = [at - origin for at, origin in zip(self.hw.center, self.origin)]
        home_xy(self.kl, 'M204 S%s\nG28 X Y\nG90\nM204 S%.3f\nG1 X%.3f Y%.3f F%d\nM400'
                % (self.limits['max_accel'], self.asked / self.k, center[0], center[1],
                   APPROACH_FEED))

    def __call__(self, direction: int, travel: float):
        """Before each attempt: the guard, the G-code offset in force (a SET_GCODE_OFFSET
        mid-run moves every G1 by it), a re-home after a failed attempt, then the head to
        the start of this move. A repeat from where the last move ended would run no distance (Klipper
        skips it) and the window would land on the standstill."""
        self.guard.check()
        if not self.stumbled:
            self.failures = 0
        self.origin = self.offset()
        if self.stumbled:
            self.rehome()
        start, _ = self.ends(travel, direction)
        self.kl.gcode('M204 S%.3f\nG1 X%.3f Y%.3f F%d\nM400'
                      % (self.asked / self.k, start[0], start[1], APPROACH_FEED))

    def script(self, distance: float, speed: float, accel: float) -> str:
        _, end = self.ends(abs(distance), 1 if distance > 0 else -1)
        return 'G1 X%.3f Y%.3f F%.3f' % (end[0], end[1], speed / self.k * 60)

    def failed(self, error: Exception):
        """After a failed attempt the next move starts from a fresh homing, up to
        FAILED_IN_A_ROW attempts in a row: a cause that fails every move would re-home
        before each one to the end of the run. A move that needs an axis homed stops the
        run at once, as a shutdown does: X or Y lost (M84, a failed G28), or Z (a mesh or
        [z_thermal_adjust] lifts it under the moves)."""
        why = ' '.join(str(error).split())
        # the display shows 'FAIL ' and the first characters of these (failure_display)
        if not set('xy') <= set(self.kl.homed_axes()):
            raise GantryUnhomed('home X and Y again: they lost their homing mid-run (%s), so the run '
                                'stopped; the X/Y motors are off%s' % (why, self.resync))
        if 'Must home axis first' in str(error):
            raise GantryUnhomed('home all axes (G28), then retry: a move needs Z homed (%s), so the '
                                'run stopped; the X/Y motors are off%s' % (why, self.resync))
        self.failures += 1
        if self.failures >= FAILED_IN_A_ROW:
            raise RunStopped('fix what fails, then run again: %d moves of %s failed in a row, '
                             'the last with: %s. The run stopped rather than re-home before every '
                             'move' % (self.failures, self.motors, why))
        self.stumbled = True

    def rehome(self):
        """G28 on the registers and mode each driver had before the run, then the run's
        own again, on each rail: a sensorless homing on a candidate may stop early or not at
        all, and a rail left on its own registers would play them in the run's place."""
        kl = self.kl
        print('Re-homing on the drivers\' own registers')
        try:
            for drive in self.drives:
                restore_chopper(kl, drive)
            for drive in self.drives:
                exit_spreadcycle(kl, drive)
            for step in self.freed:
                step()
        except KlippyError as failure:
            self.stuck(failure)
        self.home()
        try:
            free_strokes(kl, self.settings, self.limits, [])
            for drive in self.drives:
                if drive.stealth:
                    field, force, _ = drive.stealth
                    kl.gcode(tmc.set_fields_script(drive.stepper, {field: force}))
            for rail in self.rails:
                if rail.candidate:
                    set_rail_fields(kl, rail, rail.candidate)
        except KlippyError as failure:
            self.stuck(failure)
        self.stumbled = False

    @staticmethod
    def stuck(failure: KlippyError):
        refuse_after_shutdown(failure)
        # the display shows 'FAIL ' and the first characters of this (failure_display)
        raise RegistersStuck('check the X/Y drivers (DUMP_TMC): a register write around a re-home '
                             'failed (%s), so the run stopped' % ' '.join(str(failure).split()))

    def restore(self, *after):
        """Every step of the way back gets its chance (run_restore): each driver its own
        registers, then its own mode, then the limits, then the closing homing, the bed
        mesh after it; `after` last. A driver not put back, or a closing homing that
        failed, ends the run as a failure once every step had its chance, after a refusal
        of this motor too: what runs next (TUNE's next motor, DEMO's after a refusal of
        this one) would home on that driver, or on motors of a rail that may sit apart."""
        kl, drives, failed, unhomed = self.kl, self.drives, [], []

        def tracked(step):
            def run():
                try:
                    step()
                except BaseException:
                    failed.append(step)
                    raise
            return run
        run_restore(*[tracked(lambda drive=drive: restore_chopper(kl, drive)) for drive in drives],
                    *[tracked(lambda drive=drive: exit_spreadcycle(kl, drive)) for drive in drives],
                    *self.restores, lambda: self.close(failed, unhomed), *self.reloads, *after)
        flying = sys.exc_info()[1]
        if flying is not None and not refused(flying):
            return
        if failed:
            # the display shows 'FAIL ' and the first characters of this (failure_display)
            raise RegistersStuck('check the X/Y drivers (DUMP_TMC), then home by hand: one of the '
                                 'drivers of %s could not be put back after the run (above)'
                                 % self.motors)
        if unhomed:
            refuse_after_shutdown(unhomed[0])
            # the display shows 'FAIL ' and the first characters of this (failure_display)
            raise GantryUnhomed('home X and Y again: the final homing failed (%s), so the run '
                                'stopped; the X/Y motors are off%s'
                                % (' '.join(str(unhomed[0]).split()), self.resync))

    def close(self, failed: list, unhomed: list):
        """The closing homing, on the drivers' own registers. If one of them could not be
        put back, a G28 might run on a candidate's; if X or Y lost the homing, the motors
        of a rail may sit apart: the gantry goes to the user's hands instead. So it does
        when this G28 fails: after a failed homing Klipper switches every motor off."""
        if not failed and not isinstance(sys.exc_info()[1], GantryUnhomed):
            try:
                rehome_unless_hot(self.kl)
                return
            except KlippyError as failure:
                unhomed.append(failure)
        release_gantry(self.kl, cycle=True)
        if failed:
            print('Not homing: a driver could not be put back (above), and a G28 on a candidate\'s '
                  'registers can home wrong. The X/Y motors are off: check each driver with DUMP_TMC, '
                  'then home by hand')
        if unhomed:
            raise unhomed[0]
