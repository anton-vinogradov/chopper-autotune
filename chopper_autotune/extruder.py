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

import os
import statistics

from . import tmc
from .collect import Range, Screen, capture_stream, detect_hardware, refuse_if_printing, run_restore
from .dataset import load_json, save_json
from .klippy import Klippy, find_socket
from .metrics import transients, vibration_score
from .search import descent_budget, multi_start_descent, penalized_score

AMP_CAP = 3.0               # max half-stroke, mm of filament (a retraction-scale swing)
CRUISE_SEC = 2.4            # oscillation time per measurement
VALIDATE_TOP = 3            # re-measure the best candidates before recommending
STATE = os.path.expanduser('~/printer_data/config/chopper-autotune/extruder.json')


def save_winner_state(driver_name: str, winner: tmc.Chopper):
    """The extruder has no dataset like the axes do; remember the winner so SAVE_LAST=1
    can persist it later without re-running the whole heated tune."""
    save_json(STATE, {'driver': driver_name, 'fields': winner.fields()})


def load_winner_state() -> 'dict | None':
    return load_json(STATE) or None


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
    return vibration_score(samples, 0.0)['median_magnitude'], transients(samples)['clicks']


FIELD_RANGES = (Range(0, 3), Range(1, 8), Range(0, 7), Range(0, 15))   # tbl/toff/hstrt/hend


def descent(hw, kl, driver, speed: float, audible_weight: float, screen: Screen,
            rounds: int = 2, budget: 'int | None' = None) -> 'tuple[tmc.Chopper, dict]':
    """Multi-start coordinate descent (search.py — the same engine as the axis tune:
    joint tbl+toff phase and spanning hend seeds, closing the measured toff x hend
    blind spot a per-field single-start walk re-created), evaluating each candidate
    live on the extruder; the cache makes converging starts nearly free."""
    cache = {}

    def evaluate(combo):
        if combo not in cache:
            kl.gcode(tmc.set_fields_script('extruder', combo.fields()))
            magnitude, clicks = measure(hw, speed)
            cache[combo] = penalized_score(combo, [magnitude], driver, audible_weight,
                                           clicks_per_move=float(clicks))
            screen.update('Chopper E cand %d%s: %.0f'
                          % (len(cache), ' of <=%d' % budget if budget else '',
                             cache[combo]))
        return cache[combo]

    winner = multi_start_descent(driver, *FIELD_RANGES, None, tmc.KLIPPER_DEFAULT,
                                 evaluate, rounds)
    return winner, cache


def extruder_show(kl: Klippy, args, baseline_regs: dict, stealth: 'tuple | None',
                  temp: float) -> int:
    """Audible before/after for the E motor: alternate Klipper defaults and the saved
    registers at the resonance speed so the change can be heard — the E analogue of
    CHOPPER_DEMO. Requires a tuned extruder; heats like the tune does."""
    if not baseline_regs or baseline_regs == tmc.KLIPPER_DEFAULT.fields():
        raise SystemExit('the extruder is untuned (registers are Klipper defaults) — '
                         'run CHOPPER_EXTRUDER first, there is nothing to compare')
    hw = detect_hardware(kl, 'x')
    kl.subscribe_accel(hw.accel_chip)
    screen = Screen(kl, hw.display)
    speed = args.speed or 5.0
    magnitudes = {'defaults': [], 'tuned': []}
    try:
        # the heater goes on INSIDE the try — a SIGTERM during the long heat-up must
        # still reach the M104 S0 in the finally
        screen.update('Chopper E show: heating to %.0fC' % temp, force=True)
        kl.gcode('M104 S%.0f' % temp)
        kl.gcode('TEMPERATURE_WAIT SENSOR=extruder MINIMUM=%.0f' % (temp - 3))
        if stealth:
            # chopper registers only act in spreadCycle: without the force, both phases
            # would measure stealthChop vs stealthChop and report a fake 1.0x
            kl.gcode(tmc.set_fields_script('extruder', {stealth[0]: stealth[1]}))
        for round_no in (1, 2):
            for label, fields in (('defaults', tmc.KLIPPER_DEFAULT.fields()),
                                  ('tuned', baseline_regs)):
                kl.gcode(tmc.set_fields_script('extruder', fields))
                screen.update('E %d/2: %s' % (round_no, label.upper()), force=True)
                print(' round %d: %s (%s)' % (round_no, label, fields))
                magnitude, _ = measure(hw, speed)
                magnitudes[label].append(magnitude)
                print('   magnitude %.0f' % magnitude)
        d = sum(magnitudes['defaults']) / len(magnitudes['defaults'])
        t = sum(magnitudes['tuned']) / len(magnitudes['tuned'])
        from .demo import write_state
        write_state('extruder', tmc.baseline_chopper(baseline_regs), d / t)
        screen.final('Extruder: %.1fx less vibration (%.0f -> %.0f)' % (d / t, d, t))
    finally:
        run_restore(
            lambda: kl.gcode(tmc.set_fields_script('extruder', baseline_regs)),
            lambda: stealth and kl.gcode(tmc.set_fields_script('extruder', {stealth[0]: stealth[2]})),
            lambda: kl.gcode('M104 S0'),
            lambda: kl.gcode('M84'))
    return 0


def run_extruder(args) -> int:
    kl = Klippy(find_socket(args.socket)).connect()
    try:
        return extruder_tune(kl, args)
    finally:
        kl.close()


def extruder_tune(kl: Klippy, args) -> int:
    if args.save_last:                              # persist the stored winner, no re-tune
        state = load_winner_state()
        if not state:
            raise SystemExit('no stored extruder winner — run CHOPPER_EXTRUDER first')
        from .analyze import _persist, updated_config
        from .moonraker import Moonraker
        print('Persisting the stored extruder winner: %s' % state['fields'])
        _persist(Moonraker(args.url),
                 [('tmc%s extruder' % state['driver'],
                   lambda text, section: updated_config(text, section, state['fields']))],
                 'the extruder registers')
        return 0

    settings = kl.settings()
    driver, driver_name, baseline_regs, stealth, min_temp = extruder_context(settings)
    temp = float(args.temp)
    if temp < min_temp:
        raise SystemExit('TEMP=%.0f is below min_extrude_temp (%.0f) — the filament could '
                         'not move; raise TEMP or unload the filament' % (temp, min_temp))
    if args.demo:
        # the demo heats and force-moves exactly like the tune: same guards apply
        print('Extruder demo: Klipper defaults vs the saved registers at the E resonance; '
              'the hotend will HEAT to %.0fC (filament stays in)' % temp)
        if args.dry_run:
            return 0
        if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
            print('Aborted')
            return 1
        refuse_if_printing(kl)
        return extruder_show(kl, args, baseline_regs, stealth, temp)
    speeds = [float(v) for v in range(args.min_speed, args.max_speed + 1)]

    budget = descent_budget(driver, *FIELD_RANGES, None)
    print('Extruder chopper tune: tmc%s, current registers %s' % (
        driver_name, baseline_regs or tmc.KLIPPER_DEFAULT.fields()))
    print('  hotend will HEAT to %.0fC (filament stays in); scan %s mm/s on stock '
          'registers%s; then a multi-start register descent at the resonance '
          '(<=%d candidates x ~%.0fs, usually far fewer — converging starts share the cache)'
          % (temp, '%g..%g' % (speeds[0], speeds[-1]) if args.speed is None else args.speed,
             '' if args.speed is None else ' (skipped, SPEED given)', budget, CRUISE_SEC + 0.6))
    if args.dry_run:
        return 0
    if not args.yes and input('Proceed? [y/N] ').strip().lower() not in ('y', 'yes'):
        print('Aborted')
        return 1

    refuse_if_printing(kl)
    hw = detect_hardware(kl, 'x')                   # the toolhead accel chip + capture stack
    kl.subscribe_accel(hw.accel_chip)
    screen = Screen(kl, hw.display)

    try:
        # the heater goes on INSIDE the try: heating is the longest wait of the whole
        # run, and a SIGTERM/Ctrl-C there must still reach the M104 S0 in the finally
        screen.update('Chopper E: heating to %.0fC' % temp, force=True)
        print('Heating hotend to %.0fC...' % temp)
        kl.gcode('M104 S%.0f' % temp)
        kl.gcode('TEMPERATURE_WAIT SENSOR=extruder MINIMUM=%.0f' % (temp - 3))

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
        winner, cache = descent(hw, kl, driver, speed, args.audible_weight, screen,
                                budget=budget)

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
        save_winner_state(driver_name, winner)

        print('\n=== Extruder winner ===')
        print('%s  score %.0f  (f_chop %.1f kHz, h_eff %d)'
              % (winner.label(), rescored[winner],
                 tmc.chopper_freq_hz(winner, driver) / 1000.0,
                 tmc.effective_hysteresis(winner)))
        print('[tmc%s extruder]' % driver_name)
        for key, value in winner.fields().items():
            print('driver_%s: %d' % (key, value))
        screen.final('Chopper E: %s score %.0f' % (winner.label(), rescored[winner]))

        if args.save:
            from .analyze import _persist, updated_config
            from .moonraker import Moonraker
            _persist(Moonraker(args.url),
                     [('tmc%s extruder' % driver_name,
                       lambda text, section: updated_config(text, section, winner.fields()))],
                     'the extruder registers')
        else:
            print('SAVE_LAST=1 persists this winner into the config without re-tuning')
        return 0
    finally:
        run_restore(
            # a stock config carries no driver_* lines (empty baseline) — fall back to
            # Klipper defaults rather than silently leaving the last swept combo active
            lambda: kl.gcode(tmc.set_fields_script(
                'extruder', baseline_regs or tmc.KLIPPER_DEFAULT.fields())),
            lambda: stealth and kl.gcode(tmc.set_fields_script('extruder', {stealth[0]: stealth[2]})),
            lambda: kl.gcode('M104 S0'),
            lambda: kl.gcode('M84'))
