"""TMC driver models and datasheet-derived math: register constraints, chopper frequency."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

BLANK_TIME_CLOCKS = (16, 24, 36, 54)
AUDIBLE_LIMIT_HZ = 20000.0


@dataclass(frozen=True)
class Driver:
    name: str
    fclk_hz: float
    has_tpfd: bool
    # (field, value forcing spreadCycle, value restoring stealthChop); None = no stealthChop
    spreadcycle_switch: 'Optional[tuple[str, int, int]]' = None


DRIVERS = {
    '2130': Driver('2130', 13.2e6, False, ('en_pwm_mode', 0, 1)),
    '2208': Driver('2208', 12.0e6, False, ('en_spreadcycle', 1, 0)),
    '2209': Driver('2209', 12.0e6, False, ('en_spreadcycle', 1, 0)),
    '2660': Driver('2660', 15.0e6, False),
    '2240': Driver('2240', 12.5e6, True, ('en_pwm_mode', 0, 1)),
    '5160': Driver('5160', 12.0e6, True, ('en_pwm_mode', 0, 1)),
}


@dataclass(frozen=True)
class Chopper:
    tbl: int
    toff: int
    hstrt: int
    hend: int
    tpfd: Optional[int] = None

    def fields(self) -> dict:
        fields = {'tbl': self.tbl, 'toff': self.toff, 'hstrt': self.hstrt, 'hend': self.hend}
        if self.tpfd is not None:
            fields['tpfd'] = self.tpfd
        return fields

    def label(self) -> str:
        return '_'.join('%s%d' % (name, value) for name, value in self.fields().items())


def validate(c: Chopper) -> Optional[str]:
    if not 0 <= c.tbl <= 3:
        return 'tbl out of range 0..3'
    if not 1 <= c.toff <= 15:
        return 'toff out of range 1..15 (0 disables the driver)'
    if not 0 <= c.hstrt <= 7:
        return 'hstrt out of range 0..7'
    if not 0 <= c.hend <= 15:
        return 'hend out of range 0..15'
    # datasheet limit is on effective values: (hstrt+1) + (hend-3) <= 16
    if c.hstrt + c.hend > 18:
        return 'effective hstrt + hend must be <= 16 (raw sum <= 18)'
    if c.toff == 1 and c.tbl < 2:
        return 'toff=1 requires tbl >= 2 (datasheet blank time restriction)'
    if c.tpfd is not None and not 0 <= c.tpfd <= 15:
        return 'tpfd out of range 0..15'
    return None


def chopper_freq_hz(c: Chopper, driver: Driver) -> float:
    """First-order spreadCycle estimate: one phase = blank + slow decay, two phases per cycle.

    Fast decay and hysteresis time are ignored, so the real frequency is somewhat
    lower; accurate enough to flag combos falling into the audible range.
    """
    clocks = 2 * (BLANK_TIME_CLOCKS[c.tbl] + 12 + 32 * c.toff)
    return driver.fclk_hz / clocks


def is_audible(c: Chopper, driver: Driver) -> bool:
    return chopper_freq_hz(c, driver) < AUDIBLE_LIMIT_HZ


def cfg_snippet(driver: Driver, stepper: str, c: Chopper) -> str:
    lines = ['[tmc%s %s]' % (driver.name, stepper)]
    lines += ['driver_%s: %d' % (name.upper(), value) for name, value in c.fields().items()]
    return '\n'.join(lines)


def set_fields_script(stepper: str, fields: dict) -> str:
    return '\n'.join('SET_TMC_FIELD STEPPER=%s FIELD=%s VALUE=%d' % (stepper, name, value)
                     for name, value in fields.items())
