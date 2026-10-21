"""TMC driver models and datasheet-derived math: register constraints, chopper frequency."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from itertools import product
from typing import Optional

BLANK_TIME_CLOCKS = (16, 24, 36, 54)
# TMC2208/2209 use a different blank-time table than the rest of the family
BLANK_TIME_CLOCKS_220X = (16, 24, 32, 40)
# of the estimate, an upper bound: in #157 a whine stopped between 26.8 and 31.3 kHz
AUDIBLE_LIMIT_HZ = 30000.0
# what every run heard with before the limit was recorded (#157)
RECORDED_LIMIT_BEFORE_HZ = 20000.0
AUDIBLE_WEIGHT = 0.25
# from the audible limit up to this multiple of it the chopper is ultrasonic but with
# little margin; a config is nudged toward more headroom when nothing else distinguishes it
CAUTION_RATIO = 1.5
HYST_CAP = 16                   # datasheet max effective hysteresis
FREQ_MARGIN_WEIGHT = 0.05       # tie-breaker only: a real vibration win always overrides
HYST_EDGE_WEIGHT = 0.05


@dataclass(frozen=True)
class Range:
    lo: int
    hi: int

    @classmethod
    def parse(cls, text: str) -> 'Range':
        lo, _, hi = text.partition(':')
        r = cls(int(lo), int(hi or lo))
        if r.hi < r.lo:
            raise ValueError('range %s: max < min' % text)
        return r

    def values(self) -> range:
        return range(self.lo, self.hi + 1)

    def text(self) -> str:
        """As TBL= and the other range parameters take it: '1:3', or '1' for one value."""
        return '%d' % self.lo if self.lo == self.hi else '%d:%d' % (self.lo, self.hi)


TPFD_VALUES = Range(0, 15)      # the 4-bit TPFD register of the TMC2240/5160


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

    def compact(self) -> str:
        """0/2/4/7: tbl/toff/hstrt/hend, the shape the panel's register table and the
        display use (without tpfd, which a 16-character LCD line has no room for)."""
        return '/'.join(str(value) for value in (self.tbl, self.toff, self.hstrt, self.hend))


# the registers Klipper programs when the config carries no driver_* lines — they
# differ per driver (klippy/extras/tmcXXXX.py), so "stock" is a driver property
KLIPPER_DEFAULT = Chopper(2, 3, 5, 0)                 # tmc2208 / tmc2209
KLIPPER_DEFAULT_2130 = Chopper(1, 4, 0, 7)
KLIPPER_DEFAULT_2660 = Chopper(2, 4, 3, 3)
KLIPPER_DEFAULT_TPFD = Chopper(2, 3, 5, 2, 4)          # tmc2240 / tmc5160


@dataclass(frozen=True)
class Driver:
    name: str
    fclk_hz: float
    has_tpfd: bool
    # (field, value forcing spreadCycle, value restoring stealthChop); None = no stealthChop
    spreadcycle_switch: 'Optional[tuple[str, int, int]]' = None
    blank_times: 'tuple[int, ...]' = BLANK_TIME_CLOCKS
    default: Chopper = KLIPPER_DEFAULT
    # Klipper refuses the config at load above this raw hstrt + hend (tmc2660.py only):
    # a saved winner past it would keep Klipper from starting
    hysteresis_raw_max: int = 18
    # one slow decay lasts this + 32 * TOFF clocks: 24 in the TMC2208/2209/2130/5160
    # datasheets, 12 in the TMC2660's
    slow_decay_clocks: int = 24


DRIVERS = {
    '2130': Driver('2130', 13.2e6, False, ('en_pwm_mode', 0, 1), default=KLIPPER_DEFAULT_2130),
    '2208': Driver('2208', 12.0e6, False, ('en_spreadcycle', 1, 0), BLANK_TIME_CLOCKS_220X),
    '2209': Driver('2209', 12.0e6, False, ('en_spreadcycle', 1, 0), BLANK_TIME_CLOCKS_220X),
    '2660': Driver('2660', 15.0e6, False, default=KLIPPER_DEFAULT_2660, hysteresis_raw_max=15,
                   slow_decay_clocks=12),
    '2240': Driver('2240', 12.5e6, True, ('en_pwm_mode', 0, 1), default=KLIPPER_DEFAULT_TPFD),
    '5160': Driver('5160', 12.0e6, True, ('en_pwm_mode', 0, 1), default=KLIPPER_DEFAULT_TPFD),
}


def stock_chopper(driver: Driver, sweep_tpfd: bool) -> Chopper:
    """The driver's stock registers in the spelling a run uses: a run that does not
    sweep tpfd spells it None on every candidate ('leave the register alone'), and the
    stock reference must share that spelling to be found among the measurements."""
    return driver.default if sweep_tpfd else replace(driver.default, tpfd=None)


def driver_default(section_type: str) -> Chopper:
    """Stock chopper for a config section type like 'tmc2240'."""
    driver = DRIVERS.get(section_type[3:] if section_type.startswith('tmc') else section_type)
    return driver.default if driver else KLIPPER_DEFAULT


def baseline_chopper(registers: dict, tpfd: 'Optional[int]' = None,
                     default: Chopper = KLIPPER_DEFAULT) -> Chopper:
    """The chopper currently configured on a driver; missing fields fall back to
    that driver's Klipper defaults."""
    return Chopper(registers.get('tbl', default.tbl),
                   registers.get('toff', default.toff),
                   registers.get('hstrt', default.hstrt),
                   registers.get('hend', default.hend),
                   tpfd if tpfd is not None else registers.get('tpfd', default.tpfd))


def validate(c: Chopper, driver: 'Optional[Driver]' = None) -> Optional[str]:
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
    if driver is not None and c.hstrt + c.hend > driver.hysteresis_raw_max:
        return 'hstrt + hend must be <= %d on TMC%s (Klipper refuses the config otherwise)' % (
            driver.hysteresis_raw_max, driver.name)
    if c.toff == 1 and c.tbl < 2:
        return 'toff=1 requires tbl >= 2 (datasheet blank time restriction)'
    if c.tpfd is not None and c.tpfd not in TPFD_VALUES.values():
        return 'tpfd out of range 0..15'
    return None


def chopper_freq_hz(c: Chopper, driver: Driver) -> float:
    """An upper bound of the spreadCycle frequency: a cycle of two slow decays, with the
    on phase and the fast decay at their shortest, one blank time each. Both last longer
    on a real motor, the more so the higher the hysteresis, so the real frequency is
    lower: the datasheets put the slow decay at 30-70% of the cycle (#157)."""
    clocks = 2 * (driver.blank_times[c.tbl] + driver.slow_decay_clocks + 32 * c.toff)
    return driver.fclk_hz / clocks


@dataclass(frozen=True)
class Hearing:
    """Which chopper frequencies count as audible, and what that costs a combo: `weight`
    scales its score up, `skip` keeps it out of the run and out of the ranking. The
    limit applies to chopper_freq_hz, an upper bound of the real frequency, so an ear
    that hears the default limit's winners needs a higher limit (#157)."""
    limit_hz: float = AUDIBLE_LIMIT_HZ
    weight: float = AUDIBLE_WEIGHT
    skip: bool = False

    @classmethod
    def of(cls, args=None, recorded: 'Optional[dict]' = None) -> 'Hearing':
        """From the command line; what it leaves out comes from the run that recorded
        a dataset (`recorded`, its manifest), then from the defaults."""
        recorded = recorded or {}
        limit = RECORDED_LIMIT_BEFORE_HZ if recorded else AUDIBLE_LIMIT_HZ
        if 'audible_khz' not in recorded:
            # a run from before: its skip went by the estimate of the day (12 + 32 * TOFF),
            # by this one it would drop combos, its winner too, the run measured as inaudible
            recorded = dict(recorded, skip_audible=False)

        def pick(name: str, default):
            value = getattr(args, name, None)
            return value if value is not None else recorded.get(name, default)
        return cls(pick('audible_khz', limit / 1000) * 1000,
                   pick('audible_weight', AUDIBLE_WEIGHT), bool(pick('skip_audible', False)))

    def audible(self, c: Chopper, driver: Driver) -> bool:
        return chopper_freq_hz(c, driver) < self.limit_hz

    def skips(self, c: Chopper, driver: Driver) -> bool:
        return self.skip and self.audible(c, driver)

    def manifest_fields(self) -> dict:
        """What a dataset records, for of() to read back."""
        return {'audible_khz': self.limit_hz / 1000, 'audible_weight': self.weight,
                'skip_audible': self.skip}


def effective_hysteresis(c: Chopper) -> int:
    """Datasheet effective hysteresis (HSTRT+1) + (HEND-3); ≤ 16 is the legal range."""
    return (c.hstrt + 1) + (c.hend - 3)


RANGED = ('tbl', 'toff', 'hstrt', 'hend', 'tpfd')


@dataclass(frozen=True)
class Space:
    """The registers a search may try and recommend: a range per register (tpfd None: the
    run leaves that register alone) and a cap on the effective hysteresis."""
    tbl: Range
    toff: Range
    hstrt: Range
    hend: Range
    tpfd: Optional[Range] = None
    max_hysteresis: int = HYST_CAP

    @classmethod
    def of(cls, args, driver: Optional[Driver] = None) -> 'Space':
        """From the command line; a driver without TPFD gets no TPFD range."""
        tpfd = getattr(args, 'tpfd', None)
        return cls(args.tbl, args.toff, args.hstrt, args.hend,
                   tpfd if driver is None or driver.has_tpfd else None, args.max_hysteresis)

    @classmethod
    def recorded(cls, manifest: dict) -> 'Optional[Space]':
        """The space a dataset's run searched, None when it recorded none; a run from
        before the cap went up to the datasheet maximum."""
        ranges = manifest.get('ranges')
        if not ranges:
            return None
        return cls(*(Range(*ranges[name]) if ranges.get(name) else None for name in RANGED),
                   manifest.get('max_hysteresis', HYST_CAP))

    def manifest_fields(self) -> dict:
        """What a dataset records, for recorded() to read back."""
        spans = {name: getattr(self, name) for name in RANGED}
        return {'ranges': {name: [span.lo, span.hi] if span else None for name, span in spans.items()},
                'max_hysteresis': self.max_hysteresis}

    def outside(self, c: Chopper) -> Optional[str]:
        """Why c is not in the space, in the parameters that set it; None when it is."""
        reasons = ['%s %d is outside %s=%s' % (name.upper(), value, name.upper(), span.text())
                   for name, span, value in ((name, getattr(self, name), getattr(c, name))
                                             for name in RANGED)
                   if span is not None and value is not None and not span.lo <= value <= span.hi]
        if effective_hysteresis(c) > self.max_hysteresis:
            reasons.append('hysteresis %d is above MAX_HYSTERESIS=%d'
                           % (effective_hysteresis(c), self.max_hysteresis))
        return ' and '.join(reasons) or None

    def combos(self, driver: Driver) -> 'list[Chopper]':
        """The combos of the space the driver takes, tpfd left alone."""
        combos = (Chopper(*fields) for fields in product(
            self.tbl.values(), self.toff.values(), self.hstrt.values(), self.hend.values()))
        return [c for c in combos if validate(c, driver) is None and self.outside(c) is None]

    def nearest(self, c: Chopper, driver: Driver) -> Optional[Chopper]:
        """The combo of the space nearest to c, counting the steps of every register; None
        when the driver takes no combo of the space."""
        def steps(combo: Chopper) -> int:
            return abs(combo.tbl - c.tbl) + abs(combo.toff - c.toff) \
                + abs(combo.hstrt - c.hstrt) + abs(combo.hend - c.hend)
        best = min(self.combos(driver), key=steps, default=None)
        if best is None:
            return None
        if c.tpfd is None or self.tpfd is None:
            return replace(best, tpfd=c.tpfd)
        return replace(best, tpfd=min(max(c.tpfd, self.tpfd.lo), self.tpfd.hi))

    def text(self) -> str:
        """The macro parameters that set the space: TBL=1:3 TOFF=1:8 ... MAX_HYSTERESIS=15."""
        return '%s MAX_HYSTERESIS=%d' % (self.ranges_text(), self.max_hysteresis)

    def ranges_text(self) -> str:
        """The macro parameters of the ranges alone: TBL=1:3 TOFF=1:8 ... HEND=0:15."""
        return ' '.join('%s=%s' % (name.upper(), getattr(self, name).text())
                        for name in RANGED if getattr(self, name) is not None)


# not TBL 0, not the datasheet's top hysteresis 16: winners on both edges at once vibrated
# least at the resonance yet were noisy at 5 and 10 mm/s, which a run does not measure, and
# which edge makes the noise is unknown. TPFD: the sweep of CHOPPER_TUNE
SEARCH_SPACE = Space(Range(1, 3), Range(1, 8), Range(0, 7), Range(0, 15), TPFD_VALUES, 15)


def edge_penalty(c: Chopper, driver: Driver, hearing: Hearing) -> float:
    """A small preference — used as a tie-breaker in the score — for configs away from
    two edges: a chopper frequency comfortably above the audible band, and interior
    hysteresis (further from the datasheet cap = less current ripple/heat, no latent
    clicking if the run current is later raised). Weighted low, so any real vibration
    difference overrides it; it only decides when the measurement is otherwise flat
    (e.g. the whole hysteresis ladder is nearly flat at a low run current)."""
    freq = chopper_freq_hz(c, driver)
    caution = hearing.limit_hz * CAUTION_RATIO
    penalty = 0.0
    if hearing.limit_hz <= freq < caution:
        penalty += FREQ_MARGIN_WEIGHT * (caution - freq) / (caution - hearing.limit_hz)
    penalty += HYST_EDGE_WEIGHT * max(0, effective_hysteresis(c)) / HYST_CAP
    return penalty


def cfg_snippet(driver: Driver, stepper: str, c: Chopper) -> str:
    lines = ['[tmc%s %s]' % (driver.name, stepper)]
    lines += ['driver_%s: %d' % (name.upper(), value) for name, value in c.fields().items()]
    return '\n'.join(lines)


def parse_dump_field(lines: 'list[str]', register: str, field: str) -> 'int | None':
    """A field's value from DUMP_TMC output ('// GCONF:  0000000e en_pwm_mode=1 ...'):
    Klipper prefixes console lines with '// ', joins multi-line answers with '\\n// ',
    prints only the non-zero fields (a present register line without the field means
    0) and formats some values ('3(32usteps)') — the numeric head is the value. None
    when the register line is missing altogether."""
    for message in lines:
        for raw in str(message).split('\n'):
            line = raw.strip()
            if line.startswith('//'):
                line = line[2:].strip()
            if not line.startswith(register + ':'):
                continue
            for token in line.split()[1:]:
                name, sep, value = token.partition('=')
                if sep and name == field:
                    head = re.match(r'-?(0x[0-9a-fA-F]+|\d+)', value)
                    return int(head.group(0), 0) if head else None
            return 0
    return None


def set_fields_script(stepper: str, fields: dict) -> str:
    return '\n'.join('SET_TMC_FIELD STEPPER=%s FIELD=%s VALUE=%d' % (stepper, name, value)
                     for name, value in fields.items())
