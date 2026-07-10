"""Extruder chopper tuning. The extruder is the motor closest to the accelerometer on a
direct-drive head, and its chopper matters exactly where the A/B motors' does: at the
mid-band resonance (measured: filament 5 mm/s = 286 full-steps/s rang 3x above the
neighbours and separated register configs by 27%, while off-resonance stays flat).

The filament stays loaded, so the hotend is HEATED first (default 200 C — right for PLA
and its composites, workable for PETG at our tiny oscillations; ABS owners pass
TEMP=240). FORCE_MOVE bypasses Klipper's cold-extrusion protection, so the temperature
guard here is ours. The motion is a net-zero oscillation of a few mm of filament — it
never chews one spot of the filament and never drags melt into the cold zone. The
heater is switched off on every exit path.
"""
from __future__ import annotations

import statistics

import numpy as np

from . import tmc
from .collect import Screen, capture_stream, detect_hardware, refuse_if_printing, run_restore
from .klippy import Klippy, find_socket
from .metrics import transients
from .search import penalized_score

AMP_CAP = 3.0               # max half-stroke, mm of filament (a retraction-scale swing)
CRUISE_SEC = 2.4            # oscillation time per measurement
VALIDATE_TOP = 3            # re-measure the best candidates before recommending


def extruder_context(settings) -> 'tuple[tmc.Driver, str, dict, tuple | None, float]':
    """The extruder's TMC driver, its section name, current registers, the stealthChop
    switch (if configured) and min_extrude_temp."""
    for name in tmc.DRIVERS:
        section = settings.get('tmc%s extruder' % name)
        if section is not None:
            driver = tmc.DRIVERS[name]
            regs = {f: int(section['driver_%s' % f])
                    for f in ('tbl', 'toff', 'hstrt', 'hend')
                    if section.get('driver_%s' % f) is not None}
            stealth = None
            if driver.spreadcycle_switch and float(section.get('stealthchop_threshold') or 0) > 0:
                stealth = driver.spreadcycle_switch
            min_temp = float(settings.get('extruder', {}).get('min_extrude_temp', 170))
            return driver, name, regs, stealth, min_temp
    raise SystemExit('no supported TMC driver section found for the extruder')


def oscillation(speed: float, amp: float, cycles: int) -> str:
    lines = []
    for _ in range(cycles):
        lines.append('FORCE_MOVE STEPPER=extruder DISTANCE=%.2f VELOCITY=%.2f ACCEL=800'
                     % (amp, speed))
        lines.append('FORCE_MOVE STEPPER=extruder DISTANCE=-%.2f VELOCITY=%.2f ACCEL=800'
                     % (amp, speed))
    return '\n'.join(lines)


def resonant_speed(curve: 'list[tuple[float, float]]') -> float:
    """The filament speed with the strongest vibration — the regime where register
    differences are measurable at all (off-resonance the field is flat, measured)."""
    return max(curve, key=lambda point: point[1])[0]


def measure(hw, speed: float) -> 'tuple[float, int]':
    amp = min(AMP_CAP, max(0.8, speed * 0.5))
    cycles = max(2, int(CRUISE_SEC / (2 * amp / speed)))
    duration = min(2.0, cycles * 2 * amp / speed - 0.2)
    _, samples = capture_stream(hw, oscillation(speed, amp, cycles), duration)
    acc = samples[:, 1:4] - samples[:, 1:4].mean(axis=0)
    magnitude = float(np.median(np.linalg.norm(acc, axis=1)))
    return magnitude, transients(samples)['clicks']


def descent(hw, kl, driver, speed: float, audible_weight: float, screen: Screen,
            rounds: int = 2) -> 'tuple[tmc.Chopper, dict]':
    """Coordinate descent per AN-001 order, measuring each candidate live; results are
    cached per combo so revisits are free."""
    cache = {}

    def scored(combo):
        if combo not in cache:
            kl.gcode(tmc.set_fields_script('extruder', combo.fields()))
            magnitude, clicks = measure(hw, speed)
            cache[combo] = penalized_score(combo, [magnitude], driver, audible_weight,
                                           clicks_per_move=float(clicks))
            screen.update('Chopper E cand %d: %.0f' % (len(cache), cache[combo]))
        return cache[combo]

    current = tmc.KLIPPER_DEFAULT
    for _ in range(rounds):
        for field, values in (('toff', range(1, 9)), ('tbl', range(0, 4)),
                              ('hstrt', range(0, 8)), ('hend', range(0, 16))):
            candidates = []
            for value in values:
                combo = tmc.Chopper(**{**current.fields(), field: value})
                if tmc.validate(combo) is None:
                    candidates.append(combo)
            current = min(candidates, key=scored)
    return current, cache


def run_extruder(args) -> int:
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        return extruder_tune(kl, args)
    finally:
        kl.close()


def extruder_tune(kl: Klippy, args) -> int:
    settings = kl.settings()
    driver, driver_name, baseline_regs, stealth, min_temp = extruder_context(settings)
    temp = float(args.temp)
    if temp < min_temp:
        raise SystemExit('TEMP=%.0f is below min_extrude_temp (%.0f) — the filament could '
                         'not move; raise TEMP or unload the filament' % (temp, min_temp))
    speeds = [float(v) for v in range(args.min_speed, args.max_speed + 1)]

    print('Extruder chopper tune: tmc%s, current registers %s' % (
        driver_name, baseline_regs or tmc.KLIPPER_DEFAULT.fields()))
    print('  hotend will HEAT to %.0fC (filament stays in); scan %s mm/s on stock '
          'registers%s; then a register descent at the resonance'
          % (temp, '%g..%g' % (speeds[0], speeds[-1]) if args.speed is None else args.speed,
             '' if args.speed is None else ' (skipped, SPEED given)'))
    if args.dry_run:
        return 0
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1

    refuse_if_printing(kl)
    hw = detect_hardware(kl, 'x')                   # the toolhead accel chip + capture stack
    kl.subscribe_accel(hw.accel_chip)
    screen = Screen(kl, hw.display)

    screen.update('Chopper E: heating to %.0fC' % temp, force=True)
    print('Heating hotend to %.0fC...' % temp)
    kl.gcode('M104 S%.0f' % temp)
    kl.gcode('TEMPERATURE_WAIT SENSOR=extruder MINIMUM=%.0f' % (temp - 3))

    try:
        if stealth:
            field, force, _ = stealth
            print('stealthChop is configured on the extruder: forcing spreadCycle')
            kl.gcode(tmc.set_fields_script('extruder', {field: force}))

        speed = args.speed
        if speed is None:
            # scan on stock registers — a tuned chopper would mask the very peak we need
            kl.gcode(tmc.set_fields_script('extruder', tmc.KLIPPER_DEFAULT.fields()))
            print('Scanning filament speeds on Klipper-default registers...')
            curve = []
            for v in speeds:
                magnitude, _ = measure(hw, v)
                curve.append((v, magnitude))
                print('  %4.1f mm/s: %.0f' % (v, magnitude))
                screen.update('Chopper E scan %.0f mm/s' % v)
            speed = resonant_speed(curve)
            top = max(m for _, m in curve)
            flat = top < 1.5 * statistics.median(m for _, m in curve)
            print('Resonant filament speed: %.1f mm/s%s'
                  % (speed, '  (weak peak — the field may be flat here)' if flat else ''))

        print('Register descent at %.1f mm/s...' % speed)
        winner, cache = descent(hw, kl, driver, speed, args.audible_weight, screen)

        # winner's curse guard: re-measure the descent's top few before recommending
        top = sorted(cache, key=cache.get)[:VALIDATE_TOP]
        rescored = {}
        for combo in top:
            kl.gcode(tmc.set_fields_script('extruder', combo.fields()))
            scores = []
            for _ in range(2):
                magnitude, clicks = measure(hw, speed)
                scores.append(penalized_score(combo, [magnitude], driver,
                                              args.audible_weight, float(clicks)))
            rescored[combo] = statistics.mean(scores)
            print('  validate %s: %.0f' % (combo.label(), rescored[combo]))
        winner = min(rescored, key=rescored.get)

        print('\n=== Extruder winner ===')
        print('%s  score %.0f  (f_chop %.1f kHz, h_eff %d)'
              % (winner.label(), rescored[winner],
                 tmc.chopper_freq_hz(winner, driver) / 1000.0,
                 tmc.effective_hysteresis(winner)))
        print('[tmc%s extruder]' % driver_name)
        for key, value in winner.fields().items():
            print('driver_%s: %d' % (key, value))
        screen.update('Chopper E: %s score %.0f' % (winner.label(), rescored[winner]),
                      force=True)

        if args.save:
            from .analyze import _persist, updated_config
            from .moonraker import Moonraker
            _persist(Moonraker(args.url),
                     [('tmc%s extruder' % driver_name,
                       lambda text, section: updated_config(text, section, winner.fields()))],
                     'the extruder registers')
        else:
            print('Re-run with SAVE=1 to persist into the config')
        return 0
    finally:
        run_restore(
            lambda: baseline_regs and kl.gcode(tmc.set_fields_script('extruder', baseline_regs)),
            lambda: stealth and kl.gcode(tmc.set_fields_script('extruder', {stealth[0]: stealth[2]})),
            lambda: kl.gcode('M104 S0'),
            lambda: kl.gcode('M84'))
