"""Collection phase: drive the printer over a register/speed grid, record a dataset.

Runs on the printer host: talks to the klippy unix socket directly and streams
accelerometer samples over it; CSV files in /tmp are the fallback path (--csv).
"""
from __future__ import annotations

import glob
import itertools
import os
import re
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np

from . import __version__, tmc
from .dataset import Dataset, RESULTS_HOME
from .klippy import Klippy, KlippyError, find_socket
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

    @property
    def motor(self) -> str:
        return motor_label(self.stepper.rsplit('_', 1)[-1])


ACCEL_SECTIONS = ('adxl345', 'lis2dw', 'lis3dh', 'mpu9250', 'icm20948', 'bmi160')


def resolve_accel_chip(settings: dict, axis: str) -> str:
    """The chip [resonance_tester] names, in Kalico's order: a single accel_chips entry,
    the per-axis chip of a two-chip setup, accel_chip. Else the single accelerometer
    section in the config — never a guessed name (a bare 'adxl345' on a config with
    only [adxl345 hotend] would stream nothing). Several accel_chips entries measure
    together in Kalico; which one moves with this motor is not written anywhere."""
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
        return found[0]
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
    name = (settings.get(accel_chip) or {}).get('accel_name')
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
            '(stealthchop_threshold: 0 keeps it at standstill on Klipper 0.12+; '
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
    values come from the settings, where Klipper records defaults too."""
    section = settings.get('autotune_tmc ' + stepper) or {}
    lines = []
    if driver_name == '2209':
        lines.append('driver_SGTHRS: %s' % section.get('sg4_thrs', 40))
    elif driver_name != '2208':                     # 2130, 2240, 2660, 5160: SGT; 2208: none
        lines.append('driver_SGT: %s' % section.get('sgt', 1))
    if driver_name == '2240':
        # 0 too: it replaces an old line, and Klipper homes on SG4 when it is not 0
        lines.append('driver_SG4_THRS: %s' % int(section.get('sg4_thrs') or 0))
        lines.append('driver_SLOPE_CONTROL: 3')
    return lines


def autotune_tag(driver_name: str, autotune: 'str | None') -> 'str | None':
    """The goal a result records as 'measured under klipper_tmc_autotune': its CoolStep
    lowered the current. A TMC2208 has no CoolStep, so nothing to record there."""
    return None if driver_name == '2208' else autotune


def autotune_advice(settings: dict, driver_name: str, stepper: str) -> str:
    if autotune_tag(driver_name, 'auto') is None:
        # no CoolStep, no StallGuard: the result already measured stays good
        return ('klipper_tmc_autotune ([autotune_tmc %s]) writes its own tbl, toff, hstrt and hend '
                'over driver_* at every Klipper start. Keep it, or switch it off for this motor: '
                'remove [autotune_tmc %s], restart Klipper, then tune it again with SAVE=1, or save '
                'a result already measured (CHOPPER_SAVE; CHOPPER_EXTRUDER SAVE_LAST=1 for the '
                'extruder): a TMC%s has no CoolStep, so autotune did not change its current. '
                'README: With klipper_tmc_autotune' % (stepper, stepper, driver_name))
    carry = autotune_carry_over(settings, driver_name, stepper)
    return ('klipper_tmc_autotune ([autotune_tmc %s]) writes its own tbl, toff, tpfd, hstrt and '
            'hend over driver_* at every Klipper start. Keep it, or switch it off for this '
            'motor: 1) %s; 2) remove [autotune_tmc %s] and restart Klipper; 3) if this motor homes '
            'sensorless, re-tune the StallGuard threshold it homes on before anything else: the '
            'value comes from the autotune section (or its default) and ran under autotune\'s '
            'CoolStep and PWM, which go with the section, so it is only a starting point; 4) '
            'tune again%s. README: With klipper_tmc_autotune'
            % (stepper,
               'in [tmc%s %s] set %s (autotune sets these; replace any such line already there)'
               % (driver_name, stepper, ', '.join(carry)) if carry else 'nothing to carry over',
               stepper,
               '' if driver_name == '2208' else ': a run under autotune measured with its '
                                                 'CoolStep current'))


def autotune_refusal(driver_name: str, stepper: str, settings: 'dict | None' = None) -> str:
    """The display shows '<command> FAILED: ' and 120 characters: they point at the log,
    since removing the section alone would drop the StallGuard threshold with it."""
    return ('not saving [tmc%s %s]: autotune resets its chopper at start; the log says what to '
            'do. %s' % (driver_name, stepper, autotune_advice(settings or {}, driver_name, stepper)))


AUTOTUNE_MEASURED = ('klipper_tmc_autotune managed the motor during the run, and its CoolStep '
                     'lowers the current under load, while the chopper optimum depends on the '
                     'current: tune again once autotune is off for this motor (README: With '
                     'klipper_tmc_autotune; a sensorless motor needs its homing re-tuned first)')


def measured_under_autotune(driver_name: str, stepper: str) -> str:
    """The display's 120 characters point at the log, like autotune_refusal."""
    return ('not saving [tmc%s %s]: measured under autotune; the log says what to do. %s'
            % (driver_name, stepper, AUTOTUNE_MEASURED))


def refuse_autotune_save(settings: dict, driver_name: str, stepper: str):
    """Saved driver_* values on a motor klipper_tmc_autotune manages never reach the
    driver: say so instead of saving them (and restarting Klipper for nothing)."""
    if autotune_goal(settings, stepper) is not None:
        raise SystemExit(autotune_refusal(driver_name, stepper, settings))


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


def refuse_multi_motor(settings: dict, axes: str = 'xy'):
    """The tools that move or tune one motor act on stepper_x/stepper_y only. With a
    second motor on the same axis (AWD, a two-motor gantry) the twin first idles on the
    belt, then, after a re-home, holds against it, and registers, current and saves
    reach one driver of the pair (#129). Refuse before anything moves, dry run included;
    only the axes the run drives count (a dual-Y gantry can still tune X)."""
    kinematics = (settings.get('printer') or {}).get('kinematics', '')
    if kinematics.endswith('corexz'):
        # the X motors carry Z as well: a one-motor move drives the gantry up or down
        raise SystemExit('%s: the X motors move Z too; one-motor moves are not supported '
                         'there, nothing was moved' % kinematics)
    twins = [name for axis in axes for name in rail_twins(settings, axis)]
    if twins:
        raise SystemExit('%s: several motors drive one axis (AWD or a two-motor gantry); '
                         'this tool does not support that yet, nothing was moved (see issue #129)'
                         % ', '.join(twins))


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
    that runs: it counts only when it is older than the Klipper process (process_id,
    reported since v0.12), unless any_age — for a question older code answers the same
    way. The feature checks read the code itself: forks and commits between releases
    make a version number unreliable."""
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


def can_clear_homing(kl: Klippy) -> bool:
    """SET_KINEMATIC_POSITION SET_HOMED=/CLEAR_HOMED= (Klipper v0.13+, Kalico since July
    2026); older code marks EVERY axis homed with that same command. Anything unclear
    counts as unsupported."""
    return 'CLEAR_HOMED' in klipper_extra(kl, 'force_move.py')


def release_gantry(kl: Klippy, cycle: bool = False):
    """Hand the gantry to the user's hands: the gantry and head motors off, and the X/Y
    homing forgotten (hands move the head next; SET_STEPPER_ENABLE alone keeps the axes
    homed at a stale position). With every axis homed and CLEAR_HOMED in Klipper, Z
    keeps its homing (older code marks all axes homed with that command, harmless only
    then). Otherwise M84 forgets it all, and the Z motors that held go straight back on
    so a bed or gantry does not sink."""
    homed = kl.homed_axes()
    states = kl.stepper_states()
    lines = [motors_off_but_z(kl, cycle)]
    if homed == 'xyz' and can_clear_homing(kl):
        lines.append('SET_KINEMATIC_POSITION SET_HOMED= CLEAR_HOMED=XY')
    elif 'x' in homed or 'y' in homed:
        lines.append('M84')
        lines += ['SET_STEPPER_ENABLE STEPPER="%s" ENABLE=1' % name
                  for name, enabled in states.items() if name.startswith('stepper_z') and enabled]
    kl.gcode('\n'.join(lines))


class RunStopped(SystemExit):
    """A stop of the whole run, never a per-motor skip (see demo.run_demo)."""


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
        # the display shows the first 120 characters of '<command> FAILED: ' + this
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
                # the display shows the first 120 characters of '<command> FAILED: ' + this
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
    keeps holding (and its homing where release_gantry can clear X/Y alone). The on-off
    cycle: a register restore may have re-energized a driver Klipper counts as off. With Z
    unhomed by then (a failed homing), the gantry is released instead of lifting Z."""
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


def detect_hardware(kl: Klippy, axis: str, accel: bool = True) -> Hardware:
    settings = kl.settings()
    stepper = 'stepper_' + axis
    name = driver_of(settings, stepper)
    if name is None:
        raise SystemExit('no supported TMC driver section found for %s' % stepper)
    driver, section = tmc.DRIVERS[name], settings['tmc%s %s' % (name, stepper)]

    baseline = {}
    for field in ('tbl', 'toff', 'hstrt', 'hend') + (('tpfd',) if driver.has_tpfd else ()):
        value = section.get('driver_' + field)
        if value is not None:
            baseline[field] = int(value)

    spans, centers = {}, {}
    for ax in ('x', 'y'):
        rail = settings.get('stepper_' + ax)
        if rail is None or rail.get('position_max') is None:
            raise SystemExit('unsupported kinematics: no stepper_%s with position_max' % ax)
        lo, hi = float(rail.get('position_min', 0.0)), float(rail['position_max'])
        spans[ax], centers[ax] = hi - lo, (lo + hi) / 2

    kinematics = settings['printer']['kinematics']
    span = min(spans.values()) if 'core' in kinematics or 'hbot' in kinematics else spans[axis]

    stealth = None
    if driver.spreadcycle_switch and float(section.get('stealthchop_threshold') or 0) > 0:
        stealth = driver.spreadcycle_switch

    # the endstop-referee tools never stream: no demanding a chip they won't use
    chip = resolve_accel_chip(settings, axis) if accel else ''
    return Hardware(
        kl=kl,
        stepper=stepper,
        driver=driver,
        accel_chip=chip,
        kinematics=kinematics,
        axis_span=span,
        center=(centers['x'], centers['y']),
        max_accel=float(settings['printer']['max_accel']),
        baseline=baseline,
        stealth=stealth,
        # display_status is usually an implicit runtime object (auto-loaded on
        # Mainsail/Fluidd setups), not a config section — check the live objects
        display='display_status' in kl.object_list(),
        autotune=autotune_goal(settings, stepper),
        measure_chip=accel_command_chip(settings, chip) if chip else '',
    )


def build_plan(driver: tmc.Driver, tbl: Range, toff: Range, hstrt: Range, hend: Range,
               tpfd: Optional[Range], speeds: 'list[int]',
               skip_audible: bool = False) -> 'list[tuple[tmc.Chopper, int]]':
    tpfd_values = list(tpfd.values()) if tpfd is not None and driver.has_tpfd else [None]
    plan = []
    for t, o, hs, he, tp in itertools.product(tbl.values(), toff.values(), hstrt.values(),
                                              hend.values(), tpfd_values):
        combo = tmc.Chopper(t, o, hs, he, tp)
        if tmc.validate(combo, driver) is not None:
            continue
        if skip_audible and tmc.is_audible(combo, driver):
            continue
        plan.extend((combo, speed) for speed in speeds)
    return plan


def travel_for(speed: float, accel: float, measure_time: float) -> float:
    return speed * speed / accel + speed * measure_time


def fit_measure_time(speeds: 'list[int]', accel: float, limit: float,
                     requested: float) -> float:
    """The cruise time that fits the axis at the fastest requested speed. A high
    resonance speed can push the default cruise past the travel limit (measured: motor B
    at 96 mm/s needed 129 mm against a 104 mm cap, which used to abort the tune) — shrink
    instead: ranking is invariant down to ~0.4 s of cruise (window study)."""
    fit = min((limit - s * s / accel) / s for s in speeds)
    if fit >= requested:
        return requested
    if fit < MIN_MEASURE_TIME:
        raise SystemExit('even a %.2fs cruise does not fit %.0fmm at %d mm/s — raise --accel'
                         % (MIN_MEASURE_TIME, limit, max(speeds)))
    return round(fit, 2)


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


def await_flushed(pattern: str, min_span_sec: float = 0.0, timeout: float = 30.0,
                  poll: float = 0.1) -> str:
    """Wait for a Klipper accel CSV matching `pattern` to appear AND finish flushing.
    The background writer flushes in batches after the command returns — a size that
    merely stopped growing for one poll can still be a TRUNCATED capture (measured: a
    cut sweep read as a phantom 156 Hz peak). Demand the size stay stable across two
    polls AND the capture cover the expected duration."""
    deadline = time.time() + timeout
    last, stable = -1, 0
    while time.time() < deadline:
        files = glob.glob(pattern)
        if files:
            path = max(files, key=os.path.getmtime)
            size = os.path.getsize(path)
            stable = stable + 1 if size > 0 and size == last else 0
            last = size
            if stable >= 2 and capture_span(path) >= 0.9 * min_span_sec:
                return path
        time.sleep(poll)
    raise TimeoutError('capture for %s incomplete after %.0fs' % (pattern, timeout))


def wait_for_csv(name: str, min_span_sec: float = 0.0, timeout: float = CSV_WAIT_SEC) -> Path:
    try:
        # 0.05s polls: this wait sits on the hot path of EVERY csv measurement, and the
        # old fixed 0.3+0.2s settle alone cost 40-90 minutes over a full grid
        return Path(await_flushed('/tmp/*-%s.csv' % name, min_span_sec, timeout, poll=0.05))
    except TimeoutError:
        raise TimeoutError('accelerometer csv for %s did not appear/flush in /tmp' % name)


def drop_stale_csv(name: str):
    for stale in glob.glob('/tmp/*-%s.csv' % name):
        os.unlink(stale)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def park(kl: Klippy, hw: Hardware, release: bool = True):
    # a mid-run re-home keeps the motors energized: on a stepper without a dedicated
    # enable pin every disable->enable resets toff (Klipper restores its config copy,
    # klipper_tmc_autotune re-applies its value), replacing the candidate's
    home_xy(kl, 'G28 X Y\nG0 X%.1f Y%.1f F6000\nM400' % hw.center
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
    registers, spreadCycle and homing must each get their chance."""
    for step in steps:
        try:
            step()
        except BaseException as failure:
            print('restore step failed: %s' % failure)


class Screen:
    """Progress to the display via M117 (display_status -> KlipperScreen / LCD / web
    header) and to the console via a prefixed RESPOND (Mainsail / Fluidd / KlipperScreen
    console).

    The console uses a custom prefix rather than M118/`echo: `: KlipperScreen pops up a
    dismissable notification for every `echo: ` line, which during a run would cover the
    panel and swallow touch input (e.g. the Stop button). A non-`echo:` prefix still
    shows in every console but raises no popup.

    The display is only written when display_status exists; the console is attempted
    regardless and self-disables if the printer has no [respond]. Either channel
    disables itself on error so a missing one never stops a run.
    """

    CONSOLE_PREFIX = 'Chopper: '

    INTERVAL_SEC = 5.0

    def __init__(self, kl: Klippy, display: bool):
        self.kl = kl
        self.display = display
        self.console = True
        self.last = 0.0

    def update(self, text: str, force: bool = False):
        if not (self.display or self.console):
            return
        if not force and time.monotonic() - self.last < self.INTERVAL_SEC:
            return
        self.last = time.monotonic()
        if self.display:
            self.display = self._send('M117 %s' % text)
        if self.console:
            self.console = self._send('RESPOND PREFIX="%s" MSG="%s"' % (self.CONSOLE_PREFIX, text))

    def final(self, text: str):
        """End-of-run verdict: the status line as usual PLUS a KlipperScreen popup (M118's
        `echo:` raises one). Popups are banned for progress — mid-run they cover the panel
        and its Stop button — but a single one is right when the run is over."""
        self.update(text, force=True)
        self._send('M118 %s' % text)

    def _send(self, command: str) -> bool:
        # the display is a best-effort channel: no failure of it may kill a run
        try:
            self.kl.gcode(command)
            return True
        except Exception:
            return False


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
    try:
        hw.kl.gcode('\n'.join(['M400', measure, script, 'M400', measure]))
    except KlippyError:
        # Klipper toggles measurement per chip, not per name: a mid-script failure can
        # leave the chip capturing and silently corrupt every following csv
        try:
            hw.kl.gcode(measure)
        except KlippyError:
            pass
        raise
    csv_path = wait_for_csv(name, min_span_sec)
    with open(csv_path) as f:
        data = parse_accel_csv(f)
    os.unlink(csv_path)
    return data


def measure_baseline(hw: Hardware, ds: Dataset, args, done: set):
    if 'baseline' in done:
        return
    record = {'id': 'baseline', 'kind': 'baseline', 'source': args.source, 'ts': now()}
    dwell = 'G4 P%d' % int(args.measure_time * 1000)
    if args.csv:
        data = capture_csv(hw, 'baseline', dwell, args.measure_time)
    else:
        _, data = capture_stream(hw, dwell, args.measure_time)
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
                 travel: float, direction: int, accel: float, before_move) -> dict:
    """One FORCE_MOVE with capture and scoring; cruise is the steady-window duration.

    before_move is consulted per attempt: a retry re-runs the physical move, so drift
    accounting must see it too.
    """
    move = 'FORCE_MOVE STEPPER=%s DISTANCE=%.3f VELOCITY=%.1f ACCEL=%.0f' \
           % (hw.stepper, travel * direction, speed, accel)
    duration = travel / speed + speed / accel
    for attempt in (1, 2):
        try:
            before_move(direction, travel)
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
            if attempt == 2:
                record['status'] = 'failed'
                record['error'] = str(e)
                print('  %s failed: %s' % (record['id'], e))
    ds.append(record)
    return record


def run_measurement(hw: Hardware, ds: Dataset, args, combo: tmc.Chopper, speed: int,
                    iteration: int, direction: int, travel: float, accel: float,
                    before_move) -> dict:
    record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
              'source': args.source, **combo.fields(), 'speed': speed,
              'direction': direction, 'iteration': iteration, 'ts': now()}
    return measure_move(hw, ds, args, record, speed, args.measure_time, travel, direction, accel,
                        before_move)


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


def measure_combo(hw: Hardware, ds: Dataset, args, combo: tmc.Chopper, speeds: 'list[int]',
                  iterations: int, first_iteration: int, travel: float, accel: float,
                  done: set, before_move) -> 'tuple[int, int, list[float], int]':
    """The one measurement loop shared by grid, descent and validation: applies the
    registers, measures every missing (speed, iteration, direction) and reports counts."""
    hw.kl.gcode(tmc.set_fields_script(hw.stepper, combo.fields()))
    ok = failed = clicks = 0
    magnitudes = []
    for speed in speeds:
        for iteration in range(first_iteration, first_iteration + iterations):
            for direction in (1, -1):
                if measurement_id(combo, speed, iteration, direction) in done:
                    continue
                record = run_measurement(hw, ds, args, combo, speed, iteration, direction,
                                         travel, accel, before_move)
                if record['status'] == 'ok':
                    ok += 1
                    magnitudes.append(record['score']['median_magnitude'])
                    clicks += record['score'].get('clicks', 0)
                else:
                    failed += 1
    return ok, failed, magnitudes, clicks


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
    ranked = rank(aggregate(ds, False, args.trim), hw.driver, args.audible_weight)
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
    if autotune_tag(hw.driver.name, hw.autotune):
        print('\nBest measured (not for saving: %s):\n' % AUTOTUNE_MEASURED)
    elif hw.autotune is not None:
        # a TMC2208: no CoolStep tag, yet autotune writes its own chopper at every start
        print('\nBest measured (%s):\n' % autotune_advice({}, hw.driver.name, hw.stepper))
    else:
        print('\nRecommended for printer.cfg:\n')
    print(tmc.cfg_snippet(hw.driver, hw.stepper, winner['chopper']))
    screen.final(finale)
    return winner


def run_grid(kl: Klippy, hw: Hardware, ds: Dataset, args, plan, travel: float, accel: float,
             done: set, before_move, screen: Screen) -> 'tuple[int, int]':
    ok = failed = 0
    started = time.monotonic()
    for index, (combo, speed) in enumerate(plan, 1):
        if all(measurement_id(combo, speed, i, d) in done
               for i in range(args.iterations) for d in (1, -1)):
            continue
        combo_ok, combo_failed, magnitudes, clicks = measure_combo(
            hw, ds, args, combo, [speed], args.iterations, 0, travel, accel, done, before_move)
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
                          % (100 * index // len(plan), index, len(plan), eta_text(eta)))
    screen.final('Chopper grid done: %d ok, %d failed' % (ok, failed))
    return ok, failed


def validate_top(kl: Klippy, hw: Hardware, ds: Dataset, args, speeds: 'list[int]', travel: float,
                 accel: float, done: set, before_move, screen: Screen) -> 'tuple[int, int]':
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
        ranked = rank(aggregate(ds, False, args.trim), hw.driver, args.audible_weight)
        pending = [entry['chopper'] for entry in ranked[:args.validate]
                   if entry['chopper'] not in validated]
        if not pending:
            break
        print('Validating %d candidate(s) with %d extra iterations each'
              % (len(pending), VALIDATE_EXTRA_ITERATIONS))
        for combo in pending:
            combo_ok, combo_failed, _, _ = measure_combo(
                hw, ds, args, combo, speeds, VALIDATE_EXTRA_ITERATIONS, args.iterations,
                travel, accel, done, before_move)
            ok += combo_ok
            failed += combo_failed
            validated.add(combo)
    if validated:
        report_winner(hw, ds, args, screen, max(10, args.validate), trusted=validated)
    return ok, failed


def run_descent(kl: Klippy, hw: Hardware, ds: Dataset, args, tpfd: 'Range | None',
                speeds: 'list[int]', travel: float, accel: float, done: set,
                before_move, screen: Screen) -> 'tuple[int, int]':
    from .search import (dataset_history, dataset_transients, descent_budget,
                         multi_start_descent, penalized_score, seed_start)

    stats = {'ok': 0, 'failed': 0}
    budget = descent_budget(hw.driver, args.tbl, args.toff, args.hstrt, args.hend, tpfd)
    stock = tmc.stock_chopper(hw.driver, tpfd is not None)
    history = dataset_history(ds, hw.driver)
    clicks = dataset_transients(ds)

    def score_of(combo: tmc.Chopper) -> float:
        return penalized_score(combo, history[combo], hw.driver, args.audible_weight,
                               clicks[combo] / len(history[combo]))

    cache = {combo: score_of(combo) for combo in history}
    if cache:
        print('Resuming: %d candidates already measured' % len(cache))

    def measure_candidate(combo: tmc.Chopper, iterations: int, first_iteration: int = 0):
        combo_ok, combo_failed, magnitudes, combo_clicks = measure_combo(
            hw, ds, args, combo, speeds, iterations, first_iteration, travel, accel,
            done, before_move)
        stats['ok'] += combo_ok
        stats['failed'] += combo_failed
        history[combo].extend(magnitudes)
        clicks[combo] += combo_clicks

    def evaluate(combo: tmc.Chopper) -> float:
        if combo in cache:
            return cache[combo]
        if args.skip_audible and tmc.is_audible(combo, hw.driver):
            cache[combo] = float('inf')
            return cache[combo]
        measure_candidate(combo, args.iterations)
        score = score_of(combo) if history[combo] else float('inf')
        cache[combo] = score
        note = (' audible' if tmc.is_audible(combo, hw.driver) else '') \
            + (' clicks %d!' % clicks[combo] if clicks[combo] else '')
        print('  %s -> %s' % (combo.label(),
                              'failed' if score == float('inf') else '%.1f%s' % (score, note)))
        if score != float('inf'):
            # without the bound the counter reads as endless (field: run stopped by hand)
            screen.update('Chopper %s cand %d of <=%d: %.0f'
                          % (hw.motor, len(cache), budget, score))
        return score

    if args.seed_from:
        start = seed_start(Dataset.open(args.seed_from), hw.driver, args.audible_weight)
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

    if stock not in history:
        # the improvement report needs the stock reference; the descent's spanning
        # seeds usually visit it, this covers the runs where they did not (~10 s)
        print('Measuring the Klipper-default reference for the improvement report')
        measure_candidate(stock, args.iterations)

    report_winner(hw, ds, args, screen, 10, trusted=set(finalists))
    return stats['ok'], stats['failed']


def check_resume(manifest: dict, speeds: 'list[int]', accel: float, measure_time: float,
                 autotune: 'str | None' = None):
    """A resumed run must measure under the same physical conditions as the recorded one,
    or the aggregate would silently mix incomparable magnitudes under one combo key.
    klipper_tmc_autotune's CoolStep changes the current: its goal must match too (a
    manifest from before the tool recorded it has no key and is not compared)."""
    mismatched = [
        '%s: dataset %s vs current %s' % (key, stored, current)
        for key, stored, current in (('speeds', manifest.get('speeds'), speeds),
                                     ('accel', manifest.get('accel'), accel),
                                     ('measure_time', manifest.get('measure_time'), measure_time))
        if stored is not None and stored != current]
    stored = manifest.get('autotune', autotune)
    if stored != autotune:
        # the action first: the display keeps 120 characters
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


def collect(kl: Klippy, args) -> 'tuple[int, str | None]':
    args.source = 'csv' if args.csv else 'stream'
    if args.trim is None:
        args.trim = 0.25 if args.csv else 0.1

    refuse_multi_motor(kl.settings(), args.axis)
    hw = detect_hardware(kl, args.axis)
    print('Driver tmc%s on %s (motor %s), accelerometer %s, kinematics %s, baseline %s'
          % (hw.driver.name, hw.stepper, hw.motor, hw.accel_chip, hw.kinematics, hw.baseline))

    tpfd = args.tpfd
    if tpfd is not None and not hw.driver.has_tpfd:
        print('Note: tmc%s has no TPFD, skipping the TPFD sweep' % hw.driver.name)
        tpfd = None
    if args.seed_from and args.search != 'descent':
        print('Warning: --seed-from only affects --search descent, ignoring')

    speeds = list(args.speed.values())
    if min(speeds) <= 0:
        raise SystemExit('SPEED must be positive, got %s' % min(speeds))
    accel = args.accel or hw.max_accel / 10
    limit = hw.axis_span * MOVE_MARGIN
    fitted = fit_measure_time(speeds, accel, limit, args.measure_time)
    if fitted < args.measure_time:
        print('Cruise %.2fs does not fit the axis at %d mm/s: shrinking to %.2fs '
              '(ranking is window-length invariant down to ~0.4s, measured)'
              % (args.measure_time, max(speeds), fitted))
        args.measure_time = fitted
    travel = max(travel_for(s, accel, args.measure_time) for s in speeds)

    overhead = OVERHEAD_CSV_SEC if args.csv else OVERHEAD_STREAM_SEC
    per_move = args.measure_time + 2 * max(speeds) / accel + overhead
    validation_moves = args.validate * VALIDATE_EXTRA_ITERATIONS * len(speeds) * 2
    plan = []
    if args.search == 'grid':
        plan = build_plan(hw.driver, args.tbl, args.toff, args.hstrt, args.hend, tpfd, speeds,
                          args.skip_audible)
        if not plan:
            raise SystemExit('empty plan: all combinations rejected by datasheet constraints'
                             + (' or audible' if args.skip_audible else ''))
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
    ds = Dataset.create(root, {
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
        'forced_spreadcycle': bool(hw.stealth),
        'skip_audible': args.skip_audible,
        'ranges': {'tbl': [args.tbl.lo, args.tbl.hi], 'toff': [args.toff.lo, args.toff.hi],
                   'hstrt': [args.hstrt.lo, args.hstrt.hi], 'hend': [args.hend.lo, args.hend.hi],
                   'tpfd': [tpfd.lo, tpfd.hi] if tpfd else None},
        'search': args.search,
        'audible_weight': args.audible_weight,
        'accel': accel,
        'measure_time': args.measure_time,
        'trim': args.trim,
        'iterations': args.iterations,
        'validate': args.validate,
        'travel_distance': round(travel, 3),
        'speeds': speeds,
        'total_moves': n_moves,
    })
    if resuming:
        check_resume(ds.manifest(), speeds, accel, args.measure_time, autotune_tag(hw.driver.name, hw.autotune))
        if 'autotune' not in ds.manifest():
            # a dataset from before the tool recorded it: the rest is measured now
            ds.update_manifest(autotune=autotune_tag(hw.driver.name, hw.autotune))
    done = ds.done_ids()
    if done:
        print('Resuming %s: %d measurements already present' % (root, len(done)))

    print('Preparing: home XY, park at center, switch the gantry and head motors off')
    guard = ThermalGuard(kl, kl.settings())
    refuse_blind_z_hop(kl, kl.settings())       # before any motion or motor enable
    guard.preflight()                           # before the first move: not on a hot driver
    park(kl, hw)
    started = time.time()
    before_move = make_parker(kl, hw, guard)
    screen = Screen(kl, hw.display)
    try:
        measure_baseline(hw, ds, args, done)       # the noise floor: motors still off
        enter_spreadcycle(kl, hw)
        ds.update_manifest(forced_spreadcycle=bool(hw.stealth))
        if args.search == 'descent':
            ok, failed = run_descent(kl, hw, ds, args, tpfd, speeds, travel, accel, done,
                                     before_move, screen)
        else:
            ok, failed = run_grid(kl, hw, ds, args, plan, travel, accel, done, before_move, screen)
            if args.validate:
                extra_ok, extra_failed = validate_top(kl, hw, ds, args, speeds, travel, accel,
                                                      done, before_move, screen)
                ok += extra_ok
                failed += extra_failed
    finally:
        print('Restoring baseline registers, homing')
        run_restore(
            lambda: restore_chopper(kl, hw),
            lambda: exit_spreadcycle(kl, hw),
            lambda: rehome_unless_hot(kl),
            ds.flush_raw)

    print('Done in %dm: %d ok, %d failed -> %s' % ((time.time() - started) // 60, ok, failed, root))
    print('Next: chopper-autotune analyze %s' % root)
    return (0 if failed == 0 else 2), str(root)
