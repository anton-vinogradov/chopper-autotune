"""Collection phase: drive the printer over a register/speed grid, record a dataset.

Runs on the printer host: talks to the klippy unix socket directly and streams
accelerometer samples over it; CSV files are the fallback path (--csv).
"""
from __future__ import annotations

import glob
import itertools
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from . import __version__, tmc
from .dataset import Dataset, RESULTS_HOME, measured_steppers
from .klippy import ConsoleFenceLost, Klippy, KlippyError, find_socket
from .metrics import parse_accel_csv, transients, vibration_score, window
from .tmc import Range

CSV_WAIT_SEC = 30.0
MOVE_MARGIN = 0.4
MIN_MEASURE_TIME = 0.4      # ranking is window-length invariant down to here (measured)
MIN_STEADY_SAMPLES = 32
PARK_INTERVAL_MOVES = 400
OVERHEAD_STREAM_SEC = 0.3
OVERHEAD_CSV_SEC = 3.0
VALIDATE_EXTRA_ITERATIONS = 2
MAX_VALIDATE_ROUNDS = 4
KLIPPY_DIR = os.path.expanduser('~/klipper/klippy')
THERMAL_FLAGS = ('otpw', 'ot', 't120', 't143', 't150', 't157')
# TMC2240 die sensor: otpw fires at 120 C by default, shutdown near 165 C; the ADC reads
# the chip average while the output stages run hotter (datasheet), so stop earlier. In
# #133 drivers shut down at an average of 111.7 to 121.0 C, five of six with no otpw first
THERMAL_LIMIT_C = 100.0
# the die sensor's datasheet range: a reading outside it is a broken read, not a
# temperature (#133: an ADC_TEMP of 0 read as -265 C and would pass any limit)
PLAUSIBLE_DIE_C = (-40.0, 165.0)
PREFLIGHT_SEC = 1.5           # Klipper polls an enabled driver once a second


def motor_label(axis: str) -> str:
    """Name the driver we tune by its motor, not its axis. The chopper is a property of
    the motor, so on any kinematics A = stepper_x, B = stepper_y — and on CoreXY those
    two steppers *are* motors A and B (the head moves on a diagonal, not along one)."""
    return {'x': 'A', 'y': 'B'}.get(axis.lower(), axis.upper())


def coupled_xy(kinematics: str) -> bool:
    """True when stepper_x/stepper_y jointly drive X and Y (CoreXY/H-Bot, and Kalico's
    limited_corexy, CoreXY with per-axis limits): a pure X move splits the load between
    both motors, and per-motor speeds need a diagonal."""
    return kinematics in ('corexy', 'hbot', 'limited_corexy')


@dataclass
class Hardware:
    kl: Klippy
    stepper: str
    driver: tmc.Driver
    accel_chip: str
    kinematics: str
    axis_span: float
    center: 'tuple[float, float]'
    max_accel: float
    baseline: 'dict[str, int]'
    stealth: 'Optional[tuple[str, int, int]]' = None
    display: bool = False
    autotune: 'str | None' = None                  # the klipper_tmc_autotune goal, if any
    settled: bool = False                          # mode and registers to put back are known
    measure_chip: str = ''                         # CHIP= of ACCELEROMETER_MEASURE (--csv)
    twins: 'list[Hardware]' = field(default_factory=list)     # the rail's other drivers (rail_of)
    candidate: 'dict | None' = None                # the registers set_rail_fields wrote last

    @property
    def motor(self) -> str:
        return motor_label(self.stepper.rsplit('_', 1)[-1])

    @property
    def rail(self) -> 'list[Hardware]':
        """The drivers of this motor's rail, which Klipper steps together: its own, then the
        twins rail_of found."""
        return [self, *self.twins]


ACCEL_SECTIONS = ('adxl345', 'lis2dw', 'lis3dh', 'mpu9250', 'icm20948', 'bmi160')


def resolve_accel_chip(settings: dict, axis: str, sections=lambda: ()) -> str:
    """The chip [resonance_tester] names, in Kalico's order: a single accel_chips entry,
    the per-axis chip of a two-chip setup, accel_chip. Else the single accelerometer
    section in the config — never a guessed name (a bare 'adxl345' on a config with
    only [adxl345 hotend] would stream nothing). Several accel_chips entries measure
    together in Kalico; which one moves with this motor is not written anywhere.
    sections: the section names as written, asked only for that single section."""
    resonance = settings.get('resonance_tester') or {}
    chips = [chip.strip() for chip in (resonance.get('accel_chips') or '').split(',') if chip.strip()]
    if len(chips) == 1:
        return chips[0]
    # beside several accel_chips, Kalico reads accel_chip without using it: a leftover
    per_axis = resonance.get('accel_chip_' + axis) or resonance.get('accel_chip_x')
    chip = per_axis if chips else per_axis or resonance.get('accel_chip')
    if chip:
        return chip
    if chips:
        raise SystemExit('cannot pick the accelerometer: [resonance_tester] accel_chips lists '
                         'several (%s) — add accel_chip_x and accel_chip_y there to name the '
                         'chip each motor moves (Kalico keeps using accel_chips)' % ', '.join(chips))
    found = [name for name in settings if name.split()[0] in ACCEL_SECTIONS]
    if len(found) == 1:
        # settings has the names lower-cased; CHIP= and the stream take them as written
        return next((name for name in sections() if name.lower() == found[0]), found[0])
    raise SystemExit('cannot pick the accelerometer: %s — set [resonance_tester] accel_chip'
                     % ('no accelerometer section in the config' if not found
                        else 'several sections (%s)' % ', '.join(found)))


def accel_command_chip(settings: dict, accel_chip: str) -> str:
    """The CHIP= that ACCELEROMETER_MEASURE knows the chip by: the section's last word
    ('hotend' for [adxl345 hotend]). Beacon registers its accelerometer by accel_name,
    by default 'beacon' for [beacon] and 'beacon_tool' for [beacon sensor tool]."""
    parts = accel_chip.split()
    if parts[0] != 'beacon':
        return parts[-1]
    name = (settings.get(accel_chip.lower()) or {}).get('accel_name')
    if name:
        return name.split()[-1]
    return 'beacon' if len(parts) == 1 else 'beacon_' + parts[-1]


def gear_factor(ratio) -> float:
    """[stepper] gear_ratio as Klipper keeps it in settings (pairs, [[80, 16]]) or as
    written ('80:16, 2:1'): motor turns per output turn."""
    if not ratio:
        return 1.0
    pairs = [pair.split(':') for pair in ratio.split(',')] if isinstance(ratio, str) else ratio
    factor = 1.0
    for a, b in pairs:
        factor *= float(a) / float(b)
    return factor


def full_steps_per_mm(rail: dict) -> float:
    return (float(rail.get('full_steps_per_rotation') or 200) * gear_factor(rail.get('gear_ratio'))
            / float(rail['rotation_distance']))


def live_stealth(kl: Klippy, stepper: str, driver: tmc.Driver) -> 'bool | None':
    """Whether the driver has stealthChop on RIGHT NOW, read from the live GCONF: the
    config's stealthchop_threshold is not the whole truth — klipper_tmc_autotune sets the
    mode at runtime without that option (silent and autoswitch goals, and its default auto
    goal outside X/Y for motors above 0.3 Nm). Sends G-code, so callers must already be
    past the dry-run and printing guards. None = could not read."""
    if not driver.spreadcycle_switch:
        return None
    field, _, stealth_value = driver.spreadcycle_switch
    try:
        lines = kl.gcode_output('DUMP_TMC STEPPER=%s REGISTER=GCONF' % stepper)
    except KlippyError as why:
        print('could not read the %s driver mode (%s)' % (stepper, why))
        return None
    value = tmc.parse_dump_field(lines, 'GCONF', field)
    if value is None:
        print('could not read the %s driver mode: DUMP_TMC printed no GCONF line' % stepper)
    return None if value is None else value == stealth_value


# klipper_tmc_autotune's auto goal runs these in spreadCycle (performance); elsewhere
# it depends on the motor's torque, which only its motor database knows
AUTOTUNE_PERFORMANCE_MOTORS = ('stepper_x', 'stepper_y', 'dual_carriage', 'stepper_x1',
                               'stepper_y1', 'stepper_a', 'stepper_b', 'stepper_c')


def autotune_stealth(settings: dict, stepper: str) -> 'bool | None':
    """The driver mode a klipper_tmc_autotune goal sets: silent and autoswitch clear
    en_spreadcycle, performance sets it. None: no autotune, or its auto goal on a motor
    only its database can place."""
    goal = autotune_goal(settings, stepper)
    if goal in ('silent', 'autoswitch'):
        return True
    if goal == 'performance' or (goal == 'auto' and stepper in AUTOTUNE_PERFORMANCE_MOTORS):
        return False
    return None


def live_chopper(kl: Klippy, stepper: str, driver: tmc.Driver) -> 'dict | None':
    """The chopper registers the driver runs RIGHT NOW, from CHOPCONF: on a motor
    klipper_tmc_autotune manages they are its values, not the config's driver_* lines.
    Read after the stepper is enabled; toff 0 (a switched-off driver) or a missing line
    counts as unreadable. Sends G-code, like live_stealth."""
    try:
        lines = kl.gcode_output('DUMP_TMC STEPPER=%s REGISTER=CHOPCONF' % stepper)
    except KlippyError as why:
        print('could not read the %s chopper registers (%s)' % (stepper, why))
        return None
    fields = ('tbl', 'toff', 'hstrt', 'hend') + (('tpfd',) if driver.has_tpfd else ())
    values = {field: tmc.parse_dump_field(lines, 'CHOPCONF', field) for field in fields}
    if None in values.values() or not values['toff']:
        print('could not read the %s chopper registers: no usable CHOPCONF line' % stepper)
        return None
    return values


def resolve_autotune_baseline(kl: Klippy, hw: Hardware, restores: bool = True):
    """On a motor klipper_tmc_autotune manages, the run puts back what the driver ran,
    read live: its registers. The config's driver_* lines would leave the motor, and the
    re-home right after the run, on a chopper its StallGuard threshold was not tuned for.
    restores=False: a tool that leaves the registers alone (map, envelope) only reports."""
    if hw.autotune is None:
        return
    live = live_chopper(kl, hw.stepper, hw.driver)
    if live is None:
        print('%s: klipper_tmc_autotune manages it, and its registers could not be read%s'
              % (hw.stepper, ': the run ends on the config registers; restart Klipper afterwards '
                             'to get autotune\'s back' if restores else ''))
        return
    print('%s: klipper_tmc_autotune registers %s%s' % (hw.stepper, live, ', put back at the end'
                                                        if restores else ''))
    hw.baseline = live


def resolve_stealth(kl: Klippy, hw: Hardware):
    """Settle hw.stealth from the live driver: forced when the driver runs stealthChop
    OR the config asks for it — a spreadCycle left behind by a killed run still gets
    its stealthChop back at the end. Falls back to a klipper_tmc_autotune goal, then to
    the config, when unreadable."""
    if not hw.driver.spreadcycle_switch:
        return
    live = live_stealth(kl, hw.stepper, hw.driver)
    if hw.autotune is not None and live is not None:
        # autotune sets the mode at every start, whatever stealthchop_threshold says: the
        # live read is what to put back, not a mode a killed run left behind
        hw.stealth = hw.driver.spreadcycle_switch if live else None
        return
    by_autotune = autotune_stealth(kl.settings(), hw.stepper) if live is None else None
    if by_autotune is not None:
        print('%s: klipper_tmc_autotune runs it in %s' % (hw.stepper, 'stealthChop' if by_autotune
                                                          else 'spreadCycle'))
        hw.stealth = hw.driver.spreadcycle_switch if by_autotune else None
    elif live is None:
        print('trusting the config for the %s driver mode' % hw.stepper)
    elif live and not hw.stealth:
        print(unexpected_stealth(hw.stepper))
        hw.stealth = hw.driver.spreadcycle_switch


def unexpected_stealth(name: str) -> str:
    """Why a driver the config keeps in spreadCycle for moves can report stealthChop.
    Forcing spreadCycle and restoring the read value afterwards is right either way."""
    return ('%s reports stealthChop although the config does not enable it for moves '
            '(stealthchop_threshold: 0 keeps it at standstill; '
            'klipper_tmc_autotune turns it on at runtime: silent and autoswitch goals, and its '
            'default auto goal on Z and the extruder with a motor above 0.3 Nm)' % name)


def autotune_goal(settings: dict, stepper: str) -> 'str | None':
    """The tuning goal of a klipper_tmc_autotune section on this stepper, None without
    one. Autotune writes its own tbl, toff, hstrt and hend at every Klipper start
    (autotune_tmc.py tune_driver), over the driver_* values of the [tmc...] section."""
    section = settings.get('autotune_tmc ' + stepper)
    if section is None:
        return None
    return str(section.get('tuning_goal') or 'auto').lower()


def autotune_carry_over(settings: dict, driver_name: str, stepper: str) -> 'list[str]':
    """The values klipper_tmc_autotune writes at every start that the [tmc...] section
    needs once its section goes: the StallGuard thresholds sensorless homing stops on
    (Klipper's own defaults, sgthrs 0 or sgt 0, home wrong or not at all) and the
    TMC2240's fast slope. Only a starting point for the thresholds: they ran under
    autotune's CoolStep, TCOOLTHRS and PWM settings, which go with the section. Its
    values come from the settings, where Klipper records defaults too, and so does every
    option the [tmc...] section can take: a line naming another one stops Klipper at
    start (Klipper v0.13.0 has no driver_SG4_THRS)."""
    section = settings.get('autotune_tmc ' + stepper) or {}
    lines = []
    if driver_name == '2209':
        lines.append('driver_SGTHRS: %s' % section.get('sg4_thrs', 40))
    elif driver_name != '2208':                     # 2130, 2240, 2660, 5160: SGT; 2208: none
        lines.append('driver_SGT: %s' % section.get('sgt', 1))
    if driver_name == '2240':
        if 'driver_sg4_thrs' in (settings.get('tmc2240 ' + stepper) or {}):
            # 0 too: it replaces an old line, and Klipper homes on SG4 when it is not 0
            lines.append('driver_SG4_THRS: %s' % int(section.get('sg4_thrs') or 0))
        lines.append('driver_SLOPE_CONTROL: 3')
    return lines


def autotune_tag(driver_name: str, autotune: 'str | None') -> 'str | None':
    """The goal a result records as 'measured under klipper_tmc_autotune': its CoolStep
    lowered the current. A TMC2208 has no CoolStep, so nothing to record there."""
    return None if driver_name == '2208' else autotune


def tmc_sections(driver_name: str, *steppers: str) -> str:
    """'[tmc2209 stepper_x]'; a rail's sections, one for each of its drivers."""
    return ' and '.join('[tmc%s %s]' % (driver_name, stepper) for stepper in steppers)


def autotune_advice(settings: dict, driver_name: str, *steppers: str) -> str:
    """steppers: the motor's drivers, a rail's all of them, each section with the
    thresholds of its own."""
    sections = ' and '.join('[autotune_tmc %s]' % stepper for stepper in steppers)
    if autotune_tag(driver_name, 'auto') is None:
        # no CoolStep, no StallGuard: the result already measured stays good
        return ('klipper_tmc_autotune (%s) writes its own tbl, toff, hstrt and hend '
                'over driver_* at every Klipper start. Keep it, or switch it off for this motor: '
                'remove %s, restart Klipper, then tune it again with SAVE=1, or save '
                'a result already measured (CHOPPER_SAVE; CHOPPER_EXTRUDER SAVE_LAST=1 for the '
                'extruder): a TMC%s has no CoolStep, so autotune did not change its current. '
                'README: With klipper_tmc_autotune' % (sections, sections, driver_name))
    carry = [(stepper, autotune_carry_over(settings, driver_name, stepper)) for stepper in steppers]
    sets = ' and '.join('in [tmc%s %s] set %s' % (driver_name, stepper, ', '.join(lines))
                        for stepper, lines in carry if lines)
    return ('klipper_tmc_autotune (%s) writes its own tbl, toff, tpfd, hstrt and '
            'hend over driver_* at every Klipper start. Keep it, or switch it off for this '
            'motor: 1) %s; 2) remove %s and restart Klipper; 3) if this motor homes '
            'sensorless, re-tune the StallGuard threshold it homes on before anything else: the '
            'value comes from the autotune section (or its default) and ran under autotune\'s '
            'CoolStep and PWM, which go with the section, so it is only a starting point; 4) '
            'tune again%s. README: With klipper_tmc_autotune'
            % (sections,
               sets + ' (autotune sets these; replace any such line already there)' if sets
               else 'nothing to carry over',
               sections,
               '' if driver_name == '2208' else ': a run under autotune measured with its '
                                                 'CoolStep current'))


def autotune_refusal(driver_name: str, *steppers: str, settings: 'dict | None' = None) -> str:
    """The display shows the first 120 characters (failure_display): they point at the
    log, since removing the section alone would drop the StallGuard threshold with it."""
    return ('not saving %s: autotune resets its chopper at start; the log says what to do. %s'
            % (tmc_sections(driver_name, *steppers),
               autotune_advice(settings or {}, driver_name, *steppers)))


AUTOTUNE_MEASURED = ('klipper_tmc_autotune managed the motor during the run, and its CoolStep '
                     'lowers the current under load, while the chopper optimum depends on the '
                     'current: tune again once autotune is off for this motor (README: With '
                     'klipper_tmc_autotune; a sensorless motor needs its homing re-tuned first)')


def measured_under_autotune(driver_name: str, *steppers: str) -> str:
    """The display's 120 characters (failure_display) point at the log, like autotune_refusal."""
    return ('not saving %s: measured under autotune; the log says what to do. %s'
            % (tmc_sections(driver_name, *steppers), AUTOTUNE_MEASURED))


def refuse_autotune_save(settings: dict, driver_name: str, *steppers: str):
    """Saved driver_* values on a motor klipper_tmc_autotune manages never reach the
    driver: say so instead of saving them (and restarting Klipper for nothing). A rail
    saves whole, so autotune on any of its drivers refuses it, naming those."""
    managed = [stepper for stepper in steppers if autotune_goal(settings, stepper) is not None]
    if managed:
        raise SystemExit(autotune_refusal(driver_name, *managed, settings=settings))


def rail_twins(settings: dict, axis: str) -> 'list[str]':
    """Extra steppers on a rail, found the way Klipper finds them (stepper.py
    LookupMultiRail): stepper_x1, stepper_x2, ... up to the first gap."""
    twins = []
    for index in range(1, 99):
        name = 'stepper_%s%d' % (axis, index)
        if name not in settings:
            break
        twins.append(name)
    return twins


def rail_steppers(settings: dict, axis: str) -> 'list[str]':
    """Every stepper of an axis's rail: stepper_x, then its twins."""
    return ['stepper_' + axis] + rail_twins(settings, axis)


def refuse_corexz(settings: dict):
    """CoreXZ, Kalico's limited_corexz too: the X motors carry Z as well, a one-motor
    move drives the gantry up or down."""
    kinematics = (settings.get('printer') or {}).get('kinematics', '')
    if kinematics.endswith('corexz'):
        raise SystemExit('%s: the X motors move Z too; one-motor moves are not supported '
                         'there, nothing was moved' % kinematics)


def refuse_multi_motor(settings: dict, axes: str = 'xy', rails: bool = False):
    """A second motor on an axis (AWD, a two-motor gantry, #129) makes the axis a rail
    Klipper steps whole. The runs that tune a rail whole (rails: COLLECT, FIND_SPEED,
    TUNE) take the rails they support (rail.refuse_unsupported); the tools that drive or
    tune one motor refuse such an axis, where the twin would idle on the belt, hold
    against it after a re-home, and miss the registers and the current. Before anything
    moves, dry run included; only the axes the run drives count (a dual-Y gantry can
    still tune X)."""
    refuse_corexz(settings)
    twins = [name for axis in axes for name in rail_twins(settings, axis)]
    if not twins:
        return
    if rails:
        from .rail import refuse_unsupported
        for axis in axes:
            if rail_twins(settings, axis):
                refuse_unsupported(settings, axis)
        return
    raise SystemExit('not on two-motor axes yet (#129): %s share%s an axis with '
                     'stepper_x/stepper_y; of such an axis only the chopper registers are tuned '
                     'for now (CHOPPER_TUNE, CHOPPER_COLLECT, CHOPPER_FIND_SPEED). Nothing was '
                     'moved' % (', '.join(twins), '' if len(twins) > 1 else 's'))


def motors_off_but_z(kl: Klippy, cycle: bool = False) -> str:
    """Switch off the gantry and head motors: X/Y (twins included), the extruders next to
    the accelerometer, a dual carriage. Z keeps holding and its homing: M18 would unhome
    Z (see home_xy). Other steppers (a cutter, an MMU lane) are left alone. Names go in
    quotes: an extruder_stepper's name has a space. cycle: X/Y are enabled first, then
    disabled — after Klipper's own motor_off a register restore can re-energize an X/Y
    driver Klipper counts as off, and a plain ENABLE=0 is skipped for it."""
    settings = kl.settings()
    gantry = {'stepper_x', 'stepper_y'} | {twin for axis in 'xy' for twin in rail_twins(settings, axis)}
    lines = []
    for name in kl.stepper_states():
        if name in gantry or name.startswith('extruder') or name == 'dual_carriage':
            for state in (1, 0) if cycle and name in gantry else (0,):
                lines.append('SET_STEPPER_ENABLE STEPPER="%s" ENABLE=%d' % (name, state))
    return '\n'.join(lines)


_KLIPPER_EXTRAS = {}


def process_start(pid: int, proc: str = '/proc') -> 'float | None':
    """When a Linux process started, in epoch seconds (to 10 ms); None when /proc cannot
    tell. The boot time comes from uptime: /proc/stat btime is cut to whole seconds, and a
    service restart right after a git pull would read as older than the pulled files."""
    try:
        with open(os.path.join(proc, str(int(pid)), 'stat')) as stat:
            ticks = int(stat.read().rsplit(')', 1)[1].split()[19])     # field 22: starttime
        with open(os.path.join(proc, 'uptime')) as uptime:
            since_boot = float(uptime.read().split()[0])
        return time.time() - since_boot + ticks / os.sysconf('SC_CLK_TCK')
    except (OSError, TypeError, ValueError, IndexError):
        return None


def klipper_extra(kl: Klippy, filename: str, any_age: bool = False) -> str:
    """The running Klipper's own klippy/extras/<filename> (info: klipper_path), '' when it
    cannot be read. RESTART and FIRMWARE_RESTART keep the modules the process imported,
    so after a git pull without a service restart the file can be newer than the code
    that runs: it counts only when it is older than the Klipper process (process_id),
    unless any_age, which tells code pulled but not yet running from code too old (see
    require_current_klipper). The checks read the code itself: forks and commits between
    releases make a version number unreliable."""
    try:
        info = kl.info()
    except KlippyError:
        return ''
    key = (info.get('klipper_path'), info.get('process_id'), filename)
    if key not in _KLIPPER_EXTRAS:
        _KLIPPER_EXTRAS[key] = ('', False)
        try:
            path = os.path.join(info['klipper_path'], 'klippy', 'extras', filename)
            with open(path) as source:
                text = source.read()
            started = process_start(info.get('process_id'))
            _KLIPPER_EXTRAS[key] = (text, started is not None and os.path.getmtime(path) <= started)
        except (OSError, TypeError, KeyError):
            pass
    text, current = _KLIPPER_EXTRAS[key]
    return text if current or any_age else ''


def require_current_klipper(kl: Klippy):
    """The tools support the current Klipper (v0.13 and later) and Kalico (v2026.08.00
    and later), told apart by SET_KINEMATIC_POSITION CLEAR_HOMED in the running code: older
    code marks every axis homed with that command, and measures FORCE_MOVE on the
    standstill after the move (Klipper before December 2023)."""
    if 'CLEAR_HOMED' in klipper_extra(kl, 'force_move.py'):
        return
    source = klipper_extra(kl, 'force_move.py', any_age=True)
    if 'CLEAR_HOMED' in source:
        raise UnsupportedKlipper('Klipper was updated but still runs its old code: restart the '
                                 'klipper service, then retry. Nothing was moved')
    if source:
        raise UnsupportedKlipper('this Klipper is too old: chopper-autotune needs Klipper v0.13 or '
                                 'later, or Kalico v2026.08.00 or later. Nothing was moved')
    raise UnsupportedKlipper("cannot check the Klipper version: the running Klipper's "
                             'klippy/extras/force_move.py cannot be read. Nothing was moved')


def release_gantry(kl: Klippy, cycle: bool = False):
    """Hand the gantry to the user's hands: the gantry and head motors off, and the X/Y
    homing forgotten (hands move the head next; SET_STEPPER_ENABLE alone keeps the axes
    homed at a stale position). Z keeps holding and its homing, homed or not."""
    lines = [motors_off_but_z(kl, cycle)]
    if set(kl.homed_axes()) & set('xy'):
        lines.append('SET_KINEMATIC_POSITION SET_HOMED= CLEAR_HOMED=XY')
    kl.gcode('\n'.join(lines))


class RunStopped(SystemExit):
    """A stop of the whole run, never a per-motor skip (see demo.run_demo)."""


class UnsupportedKlipper(RunStopped):
    """The running Klipper is not a current one (see require_current_klipper)."""


class ZNotHomed(RunStopped):
    """A G28 X/Y would lift an unhomed Z blindly (see home_xy)."""


class PrinterBusy(RunStopped):
    """A print runs or is paused: no motor of the run may move."""


def homing_z_hop(settings: dict) -> float:
    """How far this printer's G28 lifts an UNHOMED Z on every X/Y homing, leaving it
    unhomed: [safe_z_home] z_hop, RatOS [ratos_homing] z_hop, [beacon] home_z_hop (its
    homing replaces G28 only with home_xy_position)."""
    hops = [(settings.get('safe_z_home') or {}).get('z_hop'),
            (settings.get('ratos_homing') or {}).get('z_hop')]
    beacon = settings.get('beacon') or {}
    if beacon.get('home_xy_position') is not None:
        hops.append(beacon.get('home_z_hop'))
    return max(float(hop or 0) for hop in hops)


def refuse_blind_z_hop(kl: Klippy, settings: dict):
    """Klipper's own tools answer 'Must home axis first'; homing Z here would lower the
    nozzle onto whatever stands on the bed. Checked at every job start, before motion."""
    override = settings.get('homing_override') or {}
    if override.get('set_position_z') is not None:
        print('note: [homing_override] set_position_z resets Z on every X/Y homing and may move '
              'it; home all axes (G28) after the run and watch the Z travel')
    hop = homing_z_hop(settings)
    if hop and 'z' not in kl.homed_axes():
        # the display shows 'FAIL ' and the first characters of this (failure_display)
        raise ZNotHomed('Z not homed: clear the bed, run G28, then retry (G28 X Y would lift Z %g mm '
                        'blind)' % hop)


def home_xy(kl: Klippy, script: str):
    """Every X/Y homing of the tools goes through here. With Z unhomed, a z_hop homing
    lifts Z blindly on each call and leaves it unhomed, so a job re-homing X/Y again and
    again climbed the gantry into the frame (M18 had unhomed Z; a failed G28 does too:
    Klipper then switches every motor off)."""
    refuse_blind_z_hop(kl, kl.settings())
    kl.gcode(script)


class DriverTooHot(RunStopped):
    """A driver warned of over-temperature: the run stops before the driver shuts itself
    down (GSTAT drv_err shuts Klipper down with it, #133)."""


class KlipperShutdown(RunStopped):
    """Klipper went into shutdown: every command fails from here on (#133)."""


def xy_driver_sections(settings: dict) -> 'list[str]':
    """The TMC sections of every X/Y motor, twins included."""
    steppers = {'stepper_x', 'stepper_y'} | {twin for axis in 'xy' for twin in rail_twins(settings, axis)}
    return [name for name in settings if name.startswith('tmc') and name.split(' ', 1)[-1] in steppers]


class ThermalGuard:
    """Reads the status Klipper already polls from each X/Y driver, once a second while
    the motor is enabled (None while it is off): the drv_status warning flags, and the
    die temperature a TMC2240 reports. preflight() before the first move of a run,
    check() before every move after that."""

    def __init__(self, kl: Klippy, settings: dict):
        self.kl = kl
        self.sections = xy_driver_sections(settings)
        self.subscribed = False
        # Klipper leaves a TMC2240 at slope_control 0, the slowest switching edges (the
        # most heat); klipper_tmc_autotune sets 3 "to cool down 2240s"
        self.slow = [name for name in self.sections if name.startswith('tmc2240 ')
                     and not int(settings[name].get('driver_slope_control') or 0)
                     and 'autotune_tmc ' + name.split(' ', 1)[1] not in settings]
        if self.slow:
            print('note: %s at Klipper\'s default slope_control 0, the slowest and hottest '
                  'switching edges; klipper_tmc_autotune sets 3 (driver_SLOPE_CONTROL: 3)'
                  % ', '.join(self.slow))
        # Klipper records hold_current's default, the driver's maximum: not set means the
        # full run current at standstill, and one bridge can then carry the sine peak
        held = [name for name in self.sections if name.startswith('tmc2240 ')
                and float(settings[name].get('hold_current') or float('inf'))
                >= float(settings[name].get('run_current') or 0)]
        if held:
            print('note: %s hold the full run_current at standstill (hold_current not below it): '
                  'a standstill heats the driver like a move (#133)' % ', '.join(held))
        self.unreadable = set()

    def check(self):
        if not self.sections:
            return
        if not self.subscribed:
            # a live copy: checking before every move costs no round trip to Klipper
            self.kl.subscribe_status({section: ['drv_status', 'temperature']
                                      for section in self.sections})
            self.subscribed = True
        status = self.kl.status()
        for section in self.sections:
            values = status.get(section) or {}
            flags = [flag for flag in THERMAL_FLAGS if (values.get('drv_status') or {}).get(flag)]
            temperature = values.get('temperature')
            if temperature is not None and not PLAUSIBLE_DIE_C[0] <= temperature <= PLAUSIBLE_DIE_C[1]:
                if section not in self.unreadable:
                    self.unreadable.add(section)
                    print('WARNING: %s reports a die temperature of %.0f C, which it cannot have: '
                          'the guard watches its flags only' % (section, temperature))
                temperature = None
            if flags or (temperature is not None and temperature >= THERMAL_LIMIT_C):
                why = flags[0] if flags else '%.0f C' % temperature
                hint = '; set driver_SLOPE_CONTROL: 3' if section in self.slow else ''
                # the display shows 'FAIL ' and the first characters of this (failure_display)
                raise DriverTooHot('%s overheating (%s): motors off, let it cool%s (#133)'
                                   % (section, why, hint))

    def preflight(self):
        """A driver still hot from an earlier stop must not get a fresh run. Off motors
        publish no status, so enable them (no motion), let Klipper poll, then check. Too
        hot: the gantry goes off again and Z keeps holding (release_gantry)."""
        if not self.sections:
            return
        self.kl.gcode('\n'.join('SET_STEPPER_ENABLE STEPPER=%s ENABLE=1' % name.split(' ', 1)[1]
                                for name in self.sections))
        time.sleep(PREFLIGHT_SEC)
        try:
            self.check()
        except DriverTooHot:
            release_gantry(self.kl)
            raise


def rehome_unless_hot(kl: Klippy):
    """The closing re-home of a run. After a thermal stop G28 would put the hot driver
    straight back under current: the gantry is released instead — X/Y off and their
    homing forgotten (FORCE_MOVE has left the head away from where Klipper thinks), Z
    keeps holding and its homing. The on-off cycle: a register restore may have
    re-energized a driver Klipper counts as off. With Z unhomed by then (a failed
    homing), the gantry is released instead of lifting Z."""
    if isinstance(sys.exc_info()[1], DriverTooHot):
        release_gantry(kl, cycle=True)
        return
    try:
        home_xy(kl, 'G28 X Y')
    except ZNotHomed as refused:
        print('not re-homing: %s' % refused)
        release_gantry(kl, cycle=True)


def wake_stepper(kl: Klippy, stepper: str):
    """Enable the stepper BEFORE any register write: enabling re-sends the driver
    registers, and on a stepper without a dedicated enable pin it resets toff (Klipper
    restores its own copy, klipper_tmc_autotune re-applies its value) — a write landing
    before the first move, which enables the motor, would be silently undone."""
    kl.gcode('SET_STEPPER_ENABLE STEPPER=%s ENABLE=1' % stepper)


def driver_of(settings: dict, stepper: str) -> 'str | None':
    """The supported TMC driver ('2209') of a stepper's [tmcXXXX <stepper>] section."""
    return next((name for name in tmc.DRIVERS if 'tmc%s %s' % (name, stepper) in settings), None)


def driver_config(settings: dict, stepper: str) -> dict:
    """One driver as the config sets it up, as Hardware fields: its supported TMC model,
    the chopper registers of its driver_* lines, the stealthChop the config asks for, the
    klipper_tmc_autotune goal."""
    name = driver_of(settings, stepper)
    if name is None:
        raise SystemExit('no supported TMC driver section found for %s' % stepper)
    driver, section = tmc.DRIVERS[name], settings['tmc%s %s' % (name, stepper)]

    baseline = {}
    for register in ('tbl', 'toff', 'hstrt', 'hend') + (('tpfd',) if driver.has_tpfd else ()):
        value = section.get('driver_' + register)
        if value is not None:
            baseline[register] = int(value)

    stealth = None
    if driver.spreadcycle_switch and float(section.get('stealthchop_threshold') or 0) > 0:
        stealth = driver.spreadcycle_switch
    return {'stepper': stepper, 'driver': driver, 'baseline': baseline, 'stealth': stealth,
            'autotune': autotune_goal(settings, stepper)}


def detect_hardware(kl: Klippy, axis: str, accel: bool = True) -> Hardware:
    require_current_klipper(kl)                     # every tool that moves starts here
    settings = kl.settings()
    own = driver_config(settings, 'stepper_' + axis)

    spans, centers = {}, {}
    for ax in ('x', 'y'):
        rail = settings.get('stepper_' + ax)
        if rail is None or rail.get('position_max') is None:
            raise SystemExit('unsupported kinematics: no stepper_%s with position_max' % ax)
        lo, hi = float(rail.get('position_min', 0.0)), float(rail['position_max'])
        spans[ax], centers[ax] = hi - lo, (lo + hi) / 2

    kinematics = settings['printer']['kinematics']
    span = min(spans.values()) if 'core' in kinematics or 'hbot' in kinematics else spans[axis]

    # the endstop-referee tools never stream: no demanding a chip they won't use
    chip = resolve_accel_chip(settings, axis, lambda: kl.config_sections()) if accel else ''
    return Hardware(
        kl=kl,
        accel_chip=chip,
        kinematics=kinematics,
        axis_span=span,
        center=(centers['x'], centers['y']),
        max_accel=float(settings['printer']['max_accel']),
        # display_status is usually an implicit runtime object (auto-loaded on
        # Mainsail/Fluidd setups), not a config section — check the live objects
        display='display_status' in kl.object_list(),
        measure_chip=accel_command_chip(settings, chip) if chip else '',
        **own,
    )


def rail_of(hw: Hardware) -> Hardware:
    """hw with the other drivers of its rail (rail_twins) in hw.twins, each set up from
    its own section as detect_hardware sets up the motor's. Asked for by the runs that
    write the rail's registers; the other tools keep driving stepper_x/stepper_y alone."""
    settings = hw.kl.settings()
    hw.twins = [replace(hw, twins=[], **driver_config(settings, twin))
                for twin in rail_twins(settings, hw.stepper.rsplit('_', 1)[-1])]
    return hw


def set_rail_fields(kl: Klippy, hw: Hardware, fields: dict):
    """The same registers into every driver of the rail, in one script: SET_TMC_FIELD
    writes at the toolhead's last move time, so all of them switch at one instant. A
    rail's re-home puts them back after (rail.RailMove)."""
    kl.gcode('\n'.join(tmc.set_fields_script(drive.stepper, fields) for drive in hw.rail))
    hw.candidate = dict(fields)


def build_plan(driver: tmc.Driver, tbl: Range, toff: Range, hstrt: Range, hend: Range,
               tpfd: Optional[Range], speeds: 'list[int]',
               hearing: tmc.Hearing = tmc.Hearing()) -> 'list[tuple[tmc.Chopper, int]]':
    tpfd_values = list(tpfd.values()) if tpfd is not None and driver.has_tpfd else [None]
    plan = []
    for t, o, hs, he, tp in itertools.product(tbl.values(), toff.values(), hstrt.values(),
                                              hend.values(), tpfd_values):
        combo = tmc.Chopper(t, o, hs, he, tp)
        if tmc.validate(combo, driver) is not None:
            continue
        if hearing.skips(combo, driver):
            continue
        plan.extend((combo, speed) for speed in speeds)
    return plan


def refuse_unhearable(driver: tmc.Driver, tbl: Range, toff: Range, hearing: tmc.Hearing,
                      widen: bool = False):
    """TBL and TOFF alone set the chopper frequency: with SKIP_AUDIBLE and a limit no
    pair of the ranges reaches, a run would measure nothing. Say so before anything
    moves or heats; `widen` when the user sets the ranges."""
    if not hearing.skip:
        return
    pairs = [tmc.Chopper(t, o, 0, 0) for t in tbl.values() for o in toff.values()]
    top = max((tmc.chopper_freq_hz(c, driver) for c in pairs if tmc.validate(c, driver) is None),
              default=0.0)
    if top < hearing.limit_hz:
        raise SystemExit('SKIP_AUDIBLE leaves nothing to try: the fastest TMC%s chopper the '
                         'TBL/TOFF ranges allow runs at %.1f kHz, below AUDIBLE_KHZ=%g. '
                         'Lower AUDIBLE_KHZ%s'
                         % (driver.name, top / 1000, hearing.limit_hz / 1000,
                            ' or widen TBL/TOFF' if widen else ''))


def travel_for(speed: float, accel: float, measure_time: float) -> float:
    return speed * speed / accel + speed * measure_time


def fit_measure_time(speeds: 'list[int]', accel: float, limit: float,
                     requested: float, keep_limit: bool = False) -> float:
    """The cruise time that fits the axis at the fastest requested speed. A high
    resonance speed can push the default cruise past the travel limit (measured: motor B
    at 96 mm/s needed 129 mm against a 104 mm cap, which used to abort the tune) — shrink
    instead: ranking is invariant down to ~0.4 s of cruise (window study). keep_limit: the
    limit keeps the bed edges (a rail), the cruise rounds down to stay inside it."""
    fit = min((limit - s * s / accel) / s for s in speeds)
    if fit >= requested:
        return requested
    if fit < MIN_MEASURE_TIME:
        raise SystemExit('even a %.2fs cruise does not fit %.0fmm at %d mm/s — raise --accel'
                         % (MIN_MEASURE_TIME, limit, max(speeds)))
    return int(fit * 100) / 100 if keep_limit else round(fit, 2)


def steady_window(t_end: float, speed: float, accel: float, measure_time: float,
                  guard_fraction: float) -> 'tuple[float, float]':
    """Exact cruise-phase bounds of a trapezoidal move that finished at print time t_end."""
    accel_time = speed / accel
    guard = guard_fraction * measure_time
    return t_end - accel_time - measure_time + guard, t_end - accel_time - guard


def default_dataset_root(stamp: str) -> Path:
    """Under printer_data when present, so Mainsail/Fluidd file manager shows the results."""
    base = RESULTS_HOME / 'datasets' if RESULTS_HOME.parent.is_dir() else Path('datasets')
    return base / stamp


def measurement_id(combo: tmc.Chopper, speed: int, iteration: int, direction: int) -> str:
    return '%s_v%d_i%d_%s' % (combo.label(), speed, iteration, 'fwd' if direction > 0 else 'rev')


def capture_span(path: str) -> float:
    """Seconds of data in a Klipper raw-accel CSV, reading only the file's edges (the
    last line may still be mid-write — skipped defensively)."""
    first = last = None
    with open(path, 'rb') as fh:
        for line in fh:
            if not line.startswith(b'#') and b',' in line:
                first = float(line.split(b',', 1)[0])
                break
        fh.seek(0, os.SEEK_END)
        fh.seek(-min(fh.tell(), 4096), os.SEEK_END)
        for line in reversed(fh.read().splitlines()):
            try:
                last = float(line.split(b',', 1)[0])
                break
            except (ValueError, IndexError):
                continue
    return (last - first) if first is not None and last is not None else 0.0


def capture_dirs() -> 'list[str]':
    """Where accelerometer CSVs land when the console named none: Klipper writes them to
    /tmp, Kalico to klippy's temp directory, its TMPDIR, which a tool RUN_SHELL_COMMAND
    starts inherits."""
    return sorted({os.path.realpath(d) for d in ('/tmp', tempfile.gettempdir())})


WRITTEN = re.compile(r'Writing raw accelerometer data to (.+) file')
CAPTURE_MTIME_SLACK_SEC = 1.0      # a file system with coarse mtimes rounds a fresh file down


def written_files(lines: 'list[str]') -> 'list[str]':
    """The raw files ACCELEROMETER_MEASURE and TEST_RESONANCES name in the console: where
    klippy's TMPDIR put them, also for a tool started over SSH that does not share it."""
    return [match.group(1) for match in map(WRITTEN.search, lines) if match]


def capture_pattern(written: 'list[str]', pattern: str) -> str:
    """The file the console named, else `pattern` in the capture directories."""
    return glob.escape(written[0]) if written else pattern


def capture_files(pattern: str) -> 'list[str]':
    """The files `pattern` names in every capture directory; an absolute pattern names
    its own directory alone."""
    return sorted({path for d in capture_dirs() for path in glob.glob(os.path.join(d, pattern))})


def await_flushed(pattern: str, min_span_sec: float = 0.0, timeout: float = 30.0,
                  poll: float = 0.1, newer_than: float = 0.0) -> str:
    """Wait for a Klipper accel CSV matching `pattern` to appear AND finish flushing.
    The background writer flushes in batches after the command returns — a size that
    merely stopped growing for one poll can still be a TRUNCATED capture (measured: a
    cut sweep read as a phantom 156 Hz peak). Demand the size stay stable across two
    polls AND the capture cover the expected duration. `newer_than`: a file left from an
    earlier run under the same name, where the tool cannot clean up, is not this one."""
    deadline = time.time() + timeout
    last, stable = -1, 0
    while time.time() < deadline:
        files = [path for path in capture_files(pattern) if os.path.getmtime(path) >= newer_than]
        if files:
            path = max(files, key=os.path.getmtime)
            size = os.path.getsize(path)
            stable = stable + 1 if size > 0 and size == last else 0
            last = size
            if stable >= 2 and capture_span(path) >= 0.9 * min_span_sec:
                return path
        time.sleep(poll)
    raise TimeoutError('capture for %s incomplete after %.0fs' % (pattern, timeout))


def wait_for_csv(name: str, min_span_sec: float = 0.0, timeout: float = CSV_WAIT_SEC,
                 written: 'list[str]' = (), newer_than: float = 0.0) -> Path:
    pattern = capture_pattern(list(written), '*-%s.csv' % name)
    try:
        # 0.05s polls: this wait sits on the hot path of EVERY csv measurement, and the
        # old fixed 0.3+0.2s settle alone cost 40-90 minutes over a full grid
        return Path(await_flushed(pattern, min_span_sec, timeout, poll=0.05, newer_than=newer_than))
    except TimeoutError:
        raise TimeoutError('accelerometer csv for %s did not appear/flush: %s'
                           % (name, ' or '.join(searched(pattern))))


def searched(pattern: str) -> 'list[str]':
    """Where capture_files(pattern) looks, for an error to say."""
    return sorted({os.path.join(d, pattern) for d in capture_dirs()})


def drop_stale_csv(name: str):
    for stale in capture_files('*-%s.csv' % name):
        os.unlink(stale)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def park(kl: Klippy, hw: Hardware, release: bool = True):
    # a mid-run re-home keeps the motors energized: on a stepper without a dedicated
    # enable pin every disable->enable resets toff (Klipper restores its config copy,
    # klipper_tmc_autotune re-applies its value), replacing the candidate's
    # G90: a macro may have left relative moves on, and G0 would then move by the center
    home_xy(kl, 'G28 X Y\nG90\nG0 X%.1f Y%.1f F6000\nM400' % hw.center
            + ('\n' + motors_off_but_z(kl) if release else ''))


def refuse_if_printing(kl: Klippy):
    """Tuning homes, disables motors and force-moves the axis — never during a print."""
    try:
        printing = kl.is_printing()
    except KlippyError:
        return
    if printing:
        raise PrinterBusy('printer is busy printing — not moving anything')


def run_restore(*steps):
    """Run every restore step even when one fails or a second SIGTERM lands mid-restore:
    registers, spreadCycle and homing must each get their chance. A Stop swallowed there
    (SIGTERM's integer exit, Ctrl-C) still ends the run once the steps are done, unless
    the run is on its way out already: another exception in flight keeps its own cause."""
    stop = None
    for step in steps:
        try:
            step()
        except BaseException as failure:
            print('restore step failed: %s' % failure)
            if stop is None and (isinstance(failure, KeyboardInterrupt) or isinstance(
                    failure, SystemExit) and not isinstance(failure.code, str)):
                stop = failure
    if stop is not None and sys.exc_info()[1] is None:
        raise stop


def accepted_commands(kl: Klippy) -> 'set[str] | None':
    """The G-code commands Klipper takes right now (the gcode object's status): an unknown
    one is no error there, it only prints 'Unknown command', so a channel cannot find out
    by failing. Without [respond] there is no RESPOND or M118, without [display_status]
    no M117, and after a shutdown only what runs then (M118 and RESPOND, not M117).
    None when it cannot be read: then every channel is tried."""
    try:
        status = kl.request('objects/query', {'objects': {'gcode': ['commands']}})
        return set(status['status']['gcode']['commands'])
    except Exception:
        return None


class Screen:
    """Progress to the display via M117 (display_status -> KlipperScreen / LCD / web
    header) and to the console via a prefixed RESPOND (Mainsail / Fluidd / KlipperScreen
    console).

    The console uses a custom prefix rather than M118/`echo: `: KlipperScreen pops up a
    dismissable notification for every `echo: ` line, which during a run would cover the
    panel and swallow touch input (e.g. the Stop button). A non-`echo:` prefix still
    shows in every console but raises no popup.

    The display gets `short` when a text does not fit a 16-character LCD row (a 12864's;
    a 2004 shows 20): the main part first, ASCII only (display_text). The console always
    gets the whole text.

    Each channel is used only where Klipper takes its command (accepted_commands), and
    disables itself on error so a missing one never stops a run. A stage of a longer run
    (`popup=False`: the speed scan and the register search inside CHOPPER_TUNE) ends in
    the console without a popup: the motors still have work to do.
    """

    CONSOLE_PREFIX = 'Chopper:'                     # RESPOND puts the space in itself

    INTERVAL_SEC = 5.0

    def __init__(self, kl: Klippy, display: bool, popup: bool = True):
        self.kl = kl
        commands = accepted_commands(kl)
        self.display = display and (commands is None or 'M117' in commands)
        self.console = commands is None or 'RESPOND' in commands
        self.popup = popup and (commands is None or 'M118' in commands)
        self.last = 0.0

    def update(self, text: str, force: bool = False, short: 'str | None' = None):
        if not (self.display or self.console):
            return
        if not force and time.monotonic() - self.last < self.INTERVAL_SEC:
            return
        self.last = time.monotonic()
        if self.display:
            self.display = self._send('M117 %s' % display_text(short or text))
        if self.console:
            self.console = self._send('RESPOND PREFIX="%s" MSG="%s"' % (
                self.CONSOLE_PREFIX, console_text(text)))

    def final(self, text: str, short: 'str | None' = None):
        """End-of-run verdict: the display line PLUS a KlipperScreen popup (M118's `echo:`
        raises one), which is the console line too. Popups are banned for progress —
        mid-run they cover the panel and its Stop button — but one is right at the end."""
        if self.display:
            self.display = self._send('M117 %s' % display_text(short or text))
        if self.popup:
            self._send('M118 %s' % console_safe(text))
        elif self.console:
            self.console = self._send('RESPOND PREFIX="%s" MSG="%s"' % (
                self.CONSOLE_PREFIX, console_text(text)))

    def _send(self, command: str) -> bool:
        # the display is a best-effort channel: no failure of it may kill a run
        try:
            self.kl.gcode(command)
            return True
        except Exception:
            return False


LCD_WIDTH = 16
DISPLAY_ASCII = (('\u2014', '-'), ('\u00b7', '|'), ('\u2192', '>'), ('\u2026', '...'), ('~', '-'))


def display_text(text: str) -> str:
    """A text as M117 shows it on an LCD: it draws bytes, so a character beyond ASCII
    comes out as two or three glyphs of garbage, and '~' starts glyph markup there."""
    for char, plain in DISPLAY_ASCII:
        text = text.replace(char, plain)
    return text.encode('ascii', 'ignore').decode()


def fit_row(head: str, items: 'list[str]') -> str:
    """`head` and as many of `items` as a 16-character LCD row takes, the first first;
    a '+' says some did not fit."""
    row = head
    for index, item in enumerate(items):
        longer = row + (' ' if index == 0 else ',') + item
        rest = index + 1 < len(items)
        if len(longer) + rest > LCD_WIDTH:
            return row + '+'
        row = longer
    return row


def failure_display(message: str) -> str:
    """A failure on the display: 'FAIL' and the reason, which starts with what to do or
    what failed. A 16-character LCD row keeps its first words, the Mainsail header and
    KlipperScreen's status line up to 120 characters."""
    return display_text('FAIL ' + message.split(' FAILED: ', 1)[-1])[:120]


def console_text(text: str) -> str:
    """A display text as RESPOND's MSG: without the tool's name the prefix already gives,
    and with no double quote, which would end MSG."""
    return console_safe(re.sub(r'^Chopper:?\s+', '', text)).replace('"', "'")


def console_safe(text: str) -> str:
    """A console line every console shows: KlipperScreen's reads it as Pango markup and
    drops a line a '<' or '&' makes invalid."""
    return text.replace('<', '\u2039').replace('&', 'and')


def eta_text(seconds: float) -> str:
    return '%d:%02d' % (seconds // 3600, seconds % 3600 // 60)


def enter_spreadcycle(kl: Klippy, hw: Hardware, restores: bool = True):
    """Chopper registers only act in spreadCycle; stealthChop would measure noise.
    Runs after the dry-run/printing guards: it wakes the stepper, reads the live mode
    and forces spreadCycle when needed."""
    wake_stepper(kl, hw.stepper)
    resolve_stealth(kl, hw)
    resolve_autotune_baseline(kl, hw, restores)
    hw.settled = True
    if hw.stealth:
        field, force, _ = hw.stealth
        print('stealthChop is active: forcing spreadCycle for the test')
        kl.gcode(tmc.set_fields_script(hw.stepper, {field: force}))


def untouched_autotune(hw: Hardware) -> bool:
    """A run stopped before enter_spreadcycle has written nothing: on autotune's motor
    the config's registers and mode would replace its own, so leave the driver alone.
    Other motors keep the repair of what a killed run left behind."""
    return hw.autotune is not None and not hw.settled


def restore_chopper(kl: Klippy, hw: Hardware):
    """Put back the registers the driver ran before the run (see resolve_autotune_baseline)."""
    if not untouched_autotune(hw):
        kl.gcode(tmc.set_fields_script(hw.stepper, hw.baseline or hw.driver.default.fields()))


def exit_spreadcycle(kl: Klippy, hw: Hardware):
    if untouched_autotune(hw):
        return
    if hw.stealth:
        field, _, restore = hw.stealth
        kl.gcode(tmc.set_fields_script(hw.stepper, {field: restore}))


def capture_stream(hw: Hardware, script: str, duration: float,
                   check=None) -> 'tuple[float, np.ndarray]':
    """Run script and return the samples of its last `duration` seconds. With check, a
    plain dwell (G4 P<ms>) runs in 1 s pieces with check() before each: a standstill
    window holds the motors under current too (#133). M400 makes each piece real time."""
    hw.kl.gcode('M400')
    dwell = re.fullmatch(r'G4 P(\d+)', script)
    if check is not None and dwell:
        left = int(dwell.group(1))
        while left > 0:
            check()
            hw.kl.gcode('G4 P%d\nM400' % min(1000, left))
            left -= 1000
    else:
        hw.kl.gcode(script + '\nM400')
    t_end = hw.kl.print_time()
    try:
        hw.kl.wait_for_sample(t_end)
    except KlippyError:
        # a shutdown lets the running M400 return and the stream just stops: name the cause
        try:
            info = hw.kl.info()
        except (KlippyError, OSError):
            info = {}
        if info.get('state') == 'shutdown':
            refuse_after_shutdown(KlippyError(info.get('state_message') or 'Printer is shutdown'))
        raise
    samples = hw.kl.samples_between(t_end - duration, t_end)
    if len(samples) < MIN_STEADY_SAMPLES:
        raise ValueError('only %d samples streamed for a %.2fs window' % (len(samples), duration))
    return t_end, np.array(samples, dtype=float)


def capture_csv(hw: Hardware, name: str, script: str, min_span_sec: float = 0.0) -> np.ndarray:
    drop_stale_csv(name)
    measure = 'ACCELEROMETER_MEASURE CHIP=%s NAME=%s' % (hw.measure_chip, name)
    started = time.time() - CAPTURE_MTIME_SLACK_SEC
    try:
        lines = hw.kl.gcode_output('\n'.join(['M400', measure, script, 'M400', measure]))
    except ConsoleFenceLost:
        lines = []          # the script ran to its end: the chip stopped and wrote the file
    except KlippyError:
        # Klipper toggles measurement per chip, not per name: a mid-script failure can
        # leave the chip capturing and silently corrupt every following csv
        try:
            hw.kl.gcode(measure)
        except KlippyError:
            pass
        raise
    csv_path = wait_for_csv(name, min_span_sec, written=written_files(lines), newer_than=started)
    with open(csv_path) as f:
        data = parse_accel_csv(f)
    os.unlink(csv_path)
    return data


def measure_baseline(hw: Hardware, ds: Dataset, args, done: set, *check):
    """The noise floor; check: the guard, when the motors hold under current meanwhile
    (capture_stream)."""
    if 'baseline' in done:
        return
    record = {'id': 'baseline', 'kind': 'baseline', 'source': args.source, 'ts': now()}
    dwell = 'G4 P%d' % int(args.measure_time * 1000)
    if args.csv:
        data = capture_csv(hw, 'baseline', dwell, args.measure_time)
    else:
        _, data = capture_stream(hw, dwell, args.measure_time, *check)
    record['score'] = vibration_score(data, args.trim if args.csv else 0.0)
    if not args.no_raw:
        record['raw'] = ds.store_raw_samples('baseline', data)
    record['status'] = 'ok'
    ds.append(record)
    print('Baseline noise: median magnitude %.1f' % record['score']['median_magnitude'])


def refuse_after_shutdown(error: Exception):
    """Klipper in shutdown answers every command with the same error: a run that went on
    retried 95 moves after a driver shut down (#133). Stop at the first, with its cause."""
    text = str(error)
    if 'Printer is shutdown' in text or 'FIRMWARE_RESTART' in text:
        cause = ' '.join(text.split('\n', 1)[0].replace('gcode/script failed: ', '').split())
        raise KlipperShutdown('Klipper shut down (%s): the run stops here; fix the cause, then '
                              'FIRMWARE_RESTART' % cause)


def measure_move(hw: Hardware, ds: Dataset, args, record: dict, speed: float, cruise: float,
                 travel: float, direction: int, accel: float, motion: 'ForceMove') -> dict:
    """One move of the run's motion with capture and scoring; cruise is the steady-window
    duration.

    motion is called before each attempt: a retry re-runs the physical move, so drift
    accounting must see it too; it hears of each failed attempt (motion.failed).
    """
    duration = travel / speed + speed / accel
    for attempt in (1, 2):
        try:
            motion(direction, travel)
            move = motion.script(travel * direction, speed, accel)
            if args.csv:
                data = capture_csv(hw, record['id'], move, duration)
                record['score'] = vibration_score(data, args.trim)
            else:
                overflows = hw.kl.overflows
                t_end, data = capture_stream(hw, move, duration)
                steady = steady_window(t_end, speed, accel, cruise, args.trim)
                sliced = window(data, *steady)
                if len(sliced) < MIN_STEADY_SAMPLES:
                    raise ValueError('only %d samples in the steady window' % len(sliced))
                record['steady'] = [round(steady[0], 6), round(steady[1], 6)]
                record['score'] = vibration_score(sliced, 0.0)
                lost = hw.kl.overflows - overflows
                if lost:
                    record['score']['overflows'] = lost
            # transients over the full capture: reversal clicks live outside the steady slice
            record['score'].update(transients(data))
            if not args.no_raw:
                record['raw'] = ds.store_raw_samples(record['id'], data)
            record['status'] = 'ok'
            break
        except (KlippyError, TimeoutError, ValueError, OSError) as e:
            refuse_after_shutdown(e)
            motion.failed(e)
            if attempt == 2:
                record['status'] = 'failed'
                record['error'] = str(e)
                print('  %s failed: %s' % (record['id'], e))
    ds.append(record)
    return record


def run_measurement(hw: Hardware, ds: Dataset, args, combo: tmc.Chopper, speed: int,
                    iteration: int, direction: int, travel: float, accel: float,
                    motion: 'ForceMove') -> dict:
    record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
              'source': args.source, **combo.fields(), 'speed': speed,
              'direction': direction, 'iteration': iteration, 'ts': now()}
    return measure_move(hw, ds, args, record, speed, args.measure_time, travel, direction, accel,
                        motion)


def make_parker(kl: Klippy, hw: Hardware, guard: 'ThermalGuard | None' = None):
    """Consulted before every physical move, retries included: stops on a driver
    over-temperature warning, re-homes on the periodic cadence and whenever accumulated
    net drift would leave the move no safe headroom — retries and direction-unbalanced
    resumes must never random-walk into a rail."""
    state = {'moves': 0, 'net': 0.0}
    headroom = hw.axis_span / 2 - 10.0
    guard = guard or ThermalGuard(kl, kl.settings())

    def before_move(direction: int, travel: float):
        guard.check()
        if state['moves'] >= PARK_INTERVAL_MOVES or abs(state['net'] + direction * travel) > headroom:
            print('Re-homing to reset accumulated drift')
            park(kl, hw, release=False)
            state['moves'] = 0
            state['net'] = 0.0
        state['moves'] += 1
        state['net'] += direction * travel
    return before_move


class ForceMove:
    """How a run moves the motor it measures when it is alone on its axis: a FORCE_MOVE of
    its stepper alone, out from the center of the bed and back. Klipper keeps no position
    for it: the gantry and head motors go off before the first move, and make_parker
    re-homes before the drift could reach a rail. The run takes from here the G-code of
    each move, the travel a move may take, the accel, the preparation and the way back,
    and calls it before each attempt of a move. A motor with twins moves as a rail
    (rail.RailMove), the same way round."""

    standstill = ()                     # no check while the noise floor runs: motors off
    speed_cap = float('inf')            # FORCE_MOVE knows no max_velocity

    def __init__(self, kl: Klippy, hw: Hardware):
        self.kl = kl
        self.hw = hw
        self.limit = hw.axis_span * MOVE_MARGIN
        self.default_accel = hw.max_accel / 10
        self.guard = None
        self.parker = None

    def accel(self, asked: 'float | None', top: float, cruise: float) -> float:
        return asked or self.default_accel

    def plan(self, moves: 'list[tuple[float, float]]', extension: 'int | None' = None):
        """A rail says here what it will do; one motor's plan line is the run's own."""

    def manifest_fields(self) -> dict:
        return {}

    def script(self, distance: float, speed: float, accel: float) -> str:
        return ('FORCE_MOVE STEPPER=%s DISTANCE=%.3f VELOCITY=%.1f ACCEL=%.0f'
                % (self.hw.stepper, distance, speed, accel))

    def prepare(self):
        """Home XY, park at the center and switch the gantry and head motors off, for the
        noise floor and the first move; a driver still hot from an earlier stop ends the
        run first."""
        print('Preparing: home XY, park at center, switch the gantry and head motors off')
        self.guard = ThermalGuard(self.kl, self.kl.settings())
        refuse_blind_z_hop(self.kl, self.kl.settings())     # before any motion or motor enable
        self.guard.preflight()
        park(self.kl, self.hw)
        self.parker = make_parker(self.kl, self.hw, self.guard)

    def __call__(self, direction: int, travel: float):
        self.parker(direction, travel)

    def failed(self, error: Exception):
        """The retry runs the same move again."""

    def restore(self, *after):
        """Every step of the way back gets its chance (run_restore): each driver of the
        rail its own registers, then its own mode, then the closing re-home; `after` last."""
        kl, rail = self.kl, self.hw.rail
        run_restore(*[lambda drive=drive: restore_chopper(kl, drive) for drive in rail],
                    *[lambda drive=drive: exit_spreadcycle(kl, drive) for drive in rail],
                    lambda: rehome_unless_hot(kl), *after)


def measure_combo(hw: Hardware, ds: Dataset, args, combo: tmc.Chopper, speeds: 'list[int]',
                  iterations: int, first_iteration: int, travel: float, accel: float,
                  done: set, motion: ForceMove) -> 'tuple[int, int, list[float], int]':
    """The one measurement loop shared by grid, descent and validation: applies the
    registers, measures every missing (speed, iteration, direction) and reports counts."""
    set_rail_fields(hw.kl, hw, combo.fields())
    ok = failed = clicks = 0
    magnitudes = []
    for speed in speeds:
        for iteration in range(first_iteration, first_iteration + iterations):
            for direction in (1, -1):
                if measurement_id(combo, speed, iteration, direction) in done:
                    continue
                record = run_measurement(hw, ds, args, combo, speed, iteration, direction,
                                         travel, accel, motion)
                if record['status'] == 'ok':
                    ok += 1
                    magnitudes.append(record['score']['median_magnitude'])
                    clicks += record['score'].get('clicks', 0)
                else:
                    failed += 1
    return ok, failed, magnitudes, clicks


def rail_snippet(driver: tmc.Driver, steppers: 'list[str]', combo: tmc.Chopper) -> str:
    """The config lines of a winner: a section for each driver of the motor's rail."""
    return '\n\n'.join(tmc.cfg_snippet(driver, stepper, combo) for stepper in steppers)


def shown_registers(hw: Hardware, combo: tmc.Chopper) -> tmc.Chopper:
    """The registers the panel reads back after a save: a winner spelled without tpfd
    leaves the config's TPFD line (or the stock value) in force on a TPFD driver."""
    if hw.driver.has_tpfd and combo.tpfd is None:
        return replace(combo, tpfd=hw.baseline.get('tpfd', hw.driver.default.tpfd))
    return combo


def report_winner(hw: Hardware, ds: Dataset, args, screen: Screen, top: int,
                  trusted: 'set | None' = None) -> 'dict | None':
    """Print the ranking and recommend a config. When `trusted` is given, the
    recommendation is the best-ranked combo from that (validated) set — never a
    single unmeasured lucky combo that floated to the top of the whole grid."""
    from .analyze import aggregate, print_table, rank
    ranked = rank(aggregate(ds, False, args.trim), hw.driver, tmc.Hearing.of(args))
    if not ranked:
        print('No successful measurements — nothing to recommend')
        return None
    print()
    print_table(ranked, top)
    winner = ranked[0]
    if trusted:
        validated = [entry for entry in ranked if entry['chopper'] in trusted]
        if validated:
            winner = validated[0]
    # record the recommendation: save/tune must persist THIS combo, not whatever
    # unvalidated one floats to the top of a later full re-rank
    ds.update_manifest(winner=winner['chopper'].fields())
    finale = 'Chopper: %s' % winner['chopper'].label()
    short = '%s %s' % (hw.motor, winner['chopper'].compact())
    magnitudes = {entry['chopper']: entry['magnitude'] for entry in ranked}
    stock = tmc.stock_chopper(hw.driver, getattr(args, 'tpfd', None) is not None)
    reference = magnitudes.get(stock)
    if reference and winner['chopper'] != stock:
        # the run measured Klipper defaults too, so it can say what the tuning bought —
        # same-session numbers, the panel's vibration column fills without a Show run
        quieter = reference / winner['magnitude']
        ds.update_manifest(improvement=round(quieter, 2))
        print('\nvs Klipper defaults %s: %.1fx less vibration (%.0f -> %.0f)'
              % (stock.label(), quieter, reference, winner['magnitude']))
        from .demo import write_state
        write_state(hw.stepper.rsplit('_', 1)[-1], shown_registers(hw, winner['chopper']), quieter)
        pct = round((1 - 1 / quieter) * 100)
        if pct >= 1:                                # a statistical tie is not a win
            finale += ' — %d%% less vibration' % pct
            short += ' -%d%%' % pct
    if autotune_tag(hw.driver.name, hw.autotune):
        print('\nBest measured (not for saving: %s):\n' % AUTOTUNE_MEASURED)
    elif hw.autotune is not None:
        # a TMC2208: no CoolStep tag, yet autotune writes its own chopper at every start
        print('\nBest measured (%s):\n' % autotune_advice({}, hw.driver.name,
                                                           *(drive.stepper for drive in hw.rail)))
    else:
        print('\nRecommended for printer.cfg:\n')
    print(rail_snippet(hw.driver, [drive.stepper for drive in hw.rail], winner['chopper']))
    screen.final(finale, short)
    return winner


def run_grid(kl: Klippy, hw: Hardware, ds: Dataset, args, plan, travel: float, accel: float,
             done: set, motion: ForceMove, screen: Screen) -> 'tuple[int, int]':
    ok = failed = 0
    started = time.monotonic()
    for index, (combo, speed) in enumerate(plan, 1):
        if all(measurement_id(combo, speed, i, d) in done
               for i in range(args.iterations) for d in (1, -1)):
            continue
        combo_ok, combo_failed, magnitudes, clicks = measure_combo(
            hw, ds, args, combo, [speed], args.iterations, 0, travel, accel, done, motion)
        ok += combo_ok
        failed += combo_failed
        if magnitudes:
            print('[%d/%d] %s v%d: median %.1f%s'
                  % (index, len(plan), combo.label(), speed,
                     sum(magnitudes) / len(magnitudes), ' clicks %d!' % clicks if clicks else ''))
        if ok + failed:
            remaining = (len(plan) - index) * args.iterations * 2
            eta = remaining * (time.monotonic() - started) / (ok + failed)
            screen.update('Chopper %d%% %d/%d ETA %s'
                          % (100 * index // len(plan), index, len(plan), eta_text(eta)),
                          short='%s %d%% ETA %s' % (hw.motor, 100 * index // len(plan),
                                                    eta_text(eta)))
    done_text = 'Chopper grid done: %d ok, %d failed' % (ok, failed)
    short = '%s grid %d fail' % (hw.motor, failed)
    if args.validate:
        screen.update(done_text, force=True, short=short)     # the validation still moves the motors
    else:
        screen.final(done_text, short)
    return ok, failed


def validate_top(kl: Klippy, hw: Hardware, ds: Dataset, args, speeds: 'list[int]', travel: float,
                 accel: float, done: set, motion: ForceMove, screen: Screen) -> 'tuple[int, int]':
    """Re-measure the top candidates until they hold their place.

    Validating the top-N once and re-ranking the whole grid just floats a fresh
    set of unmeasured lucky combos to the top — the winner's curse survives. So
    keep re-ranking and validating whichever top-N combos aren't validated yet
    until a full top-N is stable (or the round budget runs out), and recommend
    only from the validated set.
    """
    from .analyze import aggregate, rank
    ok = failed = 0
    validated = set()
    for _ in range(MAX_VALIDATE_ROUNDS):
        ranked = rank(aggregate(ds, False, args.trim), hw.driver, tmc.Hearing.of(args))
        pending = [entry['chopper'] for entry in ranked[:args.validate]
                   if entry['chopper'] not in validated]
        if not pending:
            break
        print('Validating %d candidate(s) with %d extra iterations each'
              % (len(pending), VALIDATE_EXTRA_ITERATIONS))
        for combo in pending:
            combo_ok, combo_failed, _, _ = measure_combo(
                hw, ds, args, combo, speeds, VALIDATE_EXTRA_ITERATIONS, args.iterations,
                travel, accel, done, motion)
            ok += combo_ok
            failed += combo_failed
            validated.add(combo)
    if validated:
        report_winner(hw, ds, args, screen, max(10, args.validate), trusted=validated)
    return ok, failed


def run_descent(kl: Klippy, hw: Hardware, ds: Dataset, args, tpfd: 'Range | None',
                speeds: 'list[int]', travel: float, accel: float, done: set,
                motion: ForceMove, screen: Screen) -> 'tuple[int, int]':
    from .search import (dataset_history, dataset_transients, descent_budget,
                         multi_start_descent, penalized_score, seed_start)

    stats = {'ok': 0, 'failed': 0}
    hearing = tmc.Hearing.of(args)
    budget = descent_budget(hw.driver, args.tbl, args.toff, args.hstrt, args.hend, tpfd)
    stock = tmc.stock_chopper(hw.driver, tpfd is not None)
    history = dataset_history(ds, hw.driver)
    clicks = dataset_transients(ds)

    def score_of(combo: tmc.Chopper) -> float:
        return penalized_score(combo, history[combo], hw.driver, hearing,
                               clicks[combo] / len(history[combo]))

    cache = {combo: score_of(combo) for combo in history}
    if cache:
        print('Resuming: %d candidates already measured' % len(cache))

    def measure_candidate(combo: tmc.Chopper, iterations: int, first_iteration: int = 0):
        combo_ok, combo_failed, magnitudes, combo_clicks = measure_combo(
            hw, ds, args, combo, speeds, iterations, first_iteration, travel, accel,
            done, motion)
        stats['ok'] += combo_ok
        stats['failed'] += combo_failed
        history[combo].extend(magnitudes)
        clicks[combo] += combo_clicks

    def evaluate(combo: tmc.Chopper) -> float:
        if combo in cache:
            return cache[combo]
        if hearing.skips(combo, hw.driver):
            cache[combo] = float('inf')
            return cache[combo]
        measure_candidate(combo, args.iterations)
        score = score_of(combo) if history[combo] else float('inf')
        cache[combo] = score
        note = (' audible' if hearing.audible(combo, hw.driver) else '') \
            + (' clicks %d!' % clicks[combo] if clicks[combo] else '')
        print('  %s -> %s' % (combo.label(),
                              'failed' if score == float('inf') else '%.1f%s' % (score, note)))
        if score != float('inf'):
            # without the bound the counter reads as endless (field: run stopped by hand)
            screen.update('Chopper %s cand %d of max %d: %.0f'
                          % (hw.motor, len(cache), budget, score),
                          short='%s %d/%d %.0f' % (hw.motor, len(cache), budget, score))
        return score

    if args.seed_from:
        start = seed_start(Dataset.open(args.seed_from), hw.driver, hearing)
        print('Seeded from %s: starting at %s' % (args.seed_from, start.label()))
    else:
        start = tmc.baseline_chopper(hw.baseline, default=stock)
    if tmc.validate(start, hw.driver) is not None:
        start = stock
    # one tpfd spelling per run: None when the register is not swept, explicit otherwise
    start = replace(start, tpfd=None if stock.tpfd is None
                    else (start.tpfd if start.tpfd is not None else stock.tpfd))

    best = multi_start_descent(hw.driver, args.tbl, args.toff, args.hstrt, args.hend, tpfd,
                               start, evaluate)
    finalists = sorted((c for c in cache if cache[c] != float('inf')), key=cache.get)[:args.validate]
    print('Descent best %s; validating top %d with extra runs' % (best.label(), len(finalists)))
    for combo in finalists:
        measure_candidate(combo, VALIDATE_EXTRA_ITERATIONS, first_iteration=args.iterations)

    if stock not in history and not hearing.skips(stock, hw.driver):
        # the improvement report needs the stock reference; the descent's spanning
        # seeds usually visit it, this covers the runs where they did not (~10 s)
        print('Measuring the Klipper-default reference for the improvement report')
        measure_candidate(stock, args.iterations)

    report_winner(hw, ds, args, screen, 10, trusted=set(finalists))
    return stats['ok'], stats['failed']


def motion_text(manifest: dict) -> str:
    steppers = measured_steppers(manifest)
    return ('%s together by G1' % ', '.join(steppers) if manifest.get('motion') == 'rail'
            else '%s alone by FORCE_MOVE' % steppers[0])


def refuse_other_motion(stored: dict, run: dict):
    """A rail's dataset resumes on the drivers it measured, moved the way it moved them: a
    rail's G1 runs every motor of it, one motor's FORCE_MOVE that one. A dataset that
    records no motion is one motor's, as every dataset from before rails (run: the manifest
    this run records). One motor's dataset resumed by one motor's run is not compared, as
    before rails (decision 19)."""
    if 'stepper' not in stored or 'rail' not in (stored.get('motion'), run.get('motion')):
        return
    if (stored.get('motion'), measured_steppers(stored)) != (run.get('motion'), measured_steppers(run)):
        # the action first: the display shows its first characters (failure_display)
        raise SystemExit('start a new dataset (no DATASET=): this one moved %s, this run moves %s, '
                         'and the two would mix under one combination'
                         % (motion_text(stored), motion_text(run)))


def check_resume(manifest: dict, speeds: 'list[int]', accel: float, measure_time: float,
                 autotune: 'str | None' = None, run: 'dict | None' = None):
    """A resumed run must measure under the same physical conditions as the recorded one,
    or the aggregate would silently mix incomparable magnitudes under one combo key: the
    same drivers moved the same way (refuse_other_motion), and klipper_tmc_autotune's
    CoolStep changes the current, so its goal must match too (a manifest from before the
    tool recorded it has no key and is not compared)."""
    if run is not None:
        refuse_other_motion(manifest, run)
    mismatched = [
        '%s: dataset %s vs current %s' % (key, stored, current)
        for key, stored, current in (('speeds', manifest.get('speeds'), speeds),
                                     ('accel', manifest.get('accel'), accel),
                                     ('measure_time', manifest.get('measure_time'), measure_time))
        if stored is not None and stored != current]
    stored = manifest.get('autotune', autotune)
    if stored != autotune:
        # the action first: the display shows its first characters (failure_display)
        raise SystemExit('refusing to resume: klipper_tmc_autotune was %s, now %s; start a new '
                         'dataset (no DATASET=)' % (stored or 'off', autotune or 'off'))
    if mismatched:
        raise SystemExit('refusing to resume with different measurement conditions (%s); '
                         'pass the original values or start a new dataset'
                         % '; '.join(mismatched))


def run_collect(args) -> int:
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        code, _ = collect(kl, args)
        return code
    finally:
        kl.close()


def collect(kl: Klippy, args, popup: bool = True) -> 'tuple[int, str | None]':
    args.source = 'csv' if args.csv else 'stream'
    if args.trim is None:
        args.trim = 0.25 if args.csv else 0.1

    from .rail import motion_for
    refuse_multi_motor(kl.settings(), args.axis, rails=True)
    hw = rail_of(detect_hardware(kl, args.axis))
    motion = motion_for(kl, hw, args)
    print('Driver tmc%s on %s (motor %s), accelerometer %s, kinematics %s, baseline %s'
          % (hw.driver.name, hw.stepper, hw.motor, hw.accel_chip, hw.kinematics, hw.baseline))

    tpfd = args.tpfd
    if tpfd is not None and not hw.driver.has_tpfd:
        print('Note: tmc%s has no TPFD, skipping the TPFD sweep' % hw.driver.name)
        tpfd = None
    if args.seed_from and args.search != 'descent':
        print('Warning: --seed-from only affects --search descent, ignoring')
    hearing = tmc.Hearing.of(args)
    refuse_unhearable(hw.driver, args.tbl, args.toff, hearing, widen=True)

    speeds = list(args.speed.values())
    if min(speeds) <= 0:
        raise SystemExit('SPEED must be positive, got %s' % min(speeds))
    accel = motion.accel(args.accel, max(speeds), args.measure_time)
    limit = motion.limit
    fitted = fit_measure_time(speeds, accel, limit, args.measure_time, keep_limit=bool(hw.twins))
    if fitted < args.measure_time:
        print('Cruise %.2fs does not fit the axis at %d mm/s: shrinking to %.2fs '
              '(ranking is window-length invariant down to ~0.4s, measured)'
              % (args.measure_time, max(speeds), fitted))
        args.measure_time = fitted
    travel = max(travel_for(s, accel, args.measure_time) for s in speeds)
    motion.plan([(s, travel_for(s, accel, args.measure_time)) for s in speeds])

    overhead = OVERHEAD_CSV_SEC if args.csv else OVERHEAD_STREAM_SEC
    per_move = args.measure_time + 2 * max(speeds) / accel + overhead
    validation_moves = args.validate * VALIDATE_EXTRA_ITERATIONS * len(speeds) * 2
    plan = []
    if args.search == 'grid':
        plan = build_plan(hw.driver, args.tbl, args.toff, args.hstrt, args.hend, tpfd, speeds,
                          hearing)
        if not plan:
            raise SystemExit('empty plan: all combinations rejected by datasheet constraints'
                             + (' or audible' if hearing.skip else ''))
        n_moves = len(plan) * args.iterations * 2 + validation_moves
        print('Plan: %d combinations x %d speeds -> %d moves of %.1fmm, capture %s, ETA %s'
              % (len(plan) // len(speeds), len(speeds), n_moves, travel, args.source,
                 eta_text(n_moves * per_move)))
    else:
        from .search import descent_budget
        budget = descent_budget(hw.driver, args.tbl, args.toff, args.hstrt, args.hend, tpfd)
        n_moves = budget * len(speeds) * args.iterations * 2 + validation_moves
        print('Plan: multi-start coordinate descent, up to %d candidates -> up to %d moves '
              'of %.1fmm, capture %s, ETA under %s'
              % (budget, n_moves, travel, args.source, eta_text(n_moves * per_move)))
    if args.dry_run:
        return 0, None
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1, None

    refuse_if_printing(kl)
    if not args.csv:
        kl.subscribe_accel(hw.accel_chip)

    root = Path(args.dataset) if args.dataset else default_dataset_root(
        '%s_%s' % (datetime.now().strftime('%Y%m%d_%H%M%S'), args.axis))
    resuming = (Path(root) / 'manifest.json').exists()
    manifest = {
        'version': __version__,
        'created': now(),
        'klippy_socket': kl.path,
        'klipper_version': kl.info().get('software_version'),
        'capture': args.source,
        'axis': args.axis,
        'stepper': hw.stepper,
        'driver': hw.driver.name,
        'fclk_hz': hw.driver.fclk_hz,
        'accel_chip': hw.accel_chip,
        'kinematics': hw.kinematics,
        'baseline_registers': hw.baseline,
        'autotune': autotune_tag(hw.driver.name, hw.autotune),
        'forced_spreadcycle': any(drive.stealth for drive in hw.rail),
        **hearing.manifest_fields(),
        'ranges': {'tbl': [args.tbl.lo, args.tbl.hi], 'toff': [args.toff.lo, args.toff.hi],
                   'hstrt': [args.hstrt.lo, args.hstrt.hi], 'hend': [args.hend.lo, args.hend.hi],
                   'tpfd': [tpfd.lo, tpfd.hi] if tpfd else None},
        'search': args.search,
        'accel': accel,
        'measure_time': args.measure_time,
        'trim': args.trim,
        'iterations': args.iterations,
        'validate': args.validate,
        'travel_distance': round(travel, 3),
        'speeds': speeds,
        'total_moves': n_moves,
        **motion.manifest_fields(),
    }
    ds = Dataset.create(root, manifest)
    if resuming:
        check_resume(ds.manifest(), speeds, accel, args.measure_time,
                     autotune_tag(hw.driver.name, hw.autotune), manifest)
        if 'autotune' not in ds.manifest():
            # a dataset from before the tool recorded it: the rest is measured now
            ds.update_manifest(autotune=autotune_tag(hw.driver.name, hw.autotune))
        # the hearing changes no measurement, only what this run tries and recommends: a
        # winner the old one picked is not this run's, until this run records its own
        if tmc.Hearing.of(recorded=ds.manifest()) != hearing:
            ds.update_manifest(winner=None, improvement=None, **hearing.manifest_fields())
    done = ds.done_ids()
    if done:
        print('Resuming %s: %d measurements already present' % (root, len(done)))

    # Screen asks Klipper first: a Stop there must not land between a rail's prepare and
    # the way back
    screen = Screen(kl, hw.display, popup)
    motion.prepare()
    try:
        started = time.time()
        # the noise floor: one motor's are off, a rail's hold (motion.standstill)
        measure_baseline(hw, ds, args, done, *motion.standstill)
        for drive in hw.rail:
            enter_spreadcycle(kl, drive)
        ds.update_manifest(forced_spreadcycle=any(drive.stealth for drive in hw.rail))
        if args.search == 'descent':
            ok, failed = run_descent(kl, hw, ds, args, tpfd, speeds, travel, accel, done,
                                     motion, screen)
        else:
            ok, failed = run_grid(kl, hw, ds, args, plan, travel, accel, done, motion, screen)
            if args.validate:
                extra_ok, extra_failed = validate_top(kl, hw, ds, args, speeds, travel, accel,
                                                      done, motion, screen)
                ok += extra_ok
                failed += extra_failed
    finally:
        print('Restoring baseline registers, homing')
        motion.restore(ds.flush_raw)

    print('Done in %dm: %d ok, %d failed -> %s' % ((time.time() - started) // 60, ok, failed, root))
    print('Next: chopper-autotune analyze %s' % root)
    return (0 if failed == 0 else 2), str(root)
