import pytest

from chopper_autotune import tmc


def test_validate_rejects_datasheet_violations():
    assert tmc.validate(tmc.Chopper(0, 0, 0, 0)) is not None
    assert tmc.validate(tmc.Chopper(0, 2, 7, 15)) is not None
    assert tmc.validate(tmc.Chopper(0, 1, 0, 0)) is not None
    assert tmc.validate(tmc.Chopper(4, 2, 0, 0)) is not None
    assert tmc.validate(tmc.Chopper(0, 2, 0, 0, tpfd=16)) is not None


def test_validate_accepts_valid_combos():
    assert tmc.validate(tmc.Chopper(2, 1, 0, 0)) is None
    assert tmc.validate(tmc.Chopper(1, 5, 4, 4)) is None
    assert tmc.validate(tmc.Chopper(0, 8, 1, 15, tpfd=0)) is None


def test_validate_hysteresis_limit_is_on_effective_values():
    # datasheet: (hstrt+1) + (hend-3) <= 16, i.e. raw sums 17 and 18 are legal
    assert tmc.validate(tmc.Chopper(0, 3, 7, 10)) is None
    assert tmc.validate(tmc.Chopper(0, 3, 7, 11)) is None
    assert tmc.validate(tmc.Chopper(0, 3, 7, 12)) is not None
    assert tmc.validate(tmc.Chopper(0, 3, 4, 15)) is not None


def test_chopper_freq_estimate():
    driver = tmc.DRIVERS['2209']
    assert tmc.chopper_freq_hz(tmc.Chopper(0, 8, 0, 0), driver) == pytest.approx(12e6 / (2 * (16 + 12 + 256)))
    assert (tmc.chopper_freq_hz(tmc.Chopper(0, 3, 0, 0), driver)
            > tmc.chopper_freq_hz(tmc.Chopper(0, 8, 0, 0), driver))
    assert tmc.is_audible(tmc.Chopper(0, 10, 0, 0), driver)
    assert not tmc.is_audible(tmc.Chopper(0, 5, 0, 0), driver)


def test_blank_times_are_per_driver():
    # 2208/2209 datasheet: 16/24/32/40 clocks; 2130/2240/5160/2660: 16/24/36/54
    assert tmc.chopper_freq_hz(tmc.Chopper(2, 1, 0, 0), tmc.DRIVERS['2209']) \
        == pytest.approx(12e6 / (2 * (32 + 12 + 32)))
    assert tmc.chopper_freq_hz(tmc.Chopper(3, 1, 0, 0), tmc.DRIVERS['2209']) \
        == pytest.approx(12e6 / (2 * (40 + 12 + 32)))
    assert tmc.chopper_freq_hz(tmc.Chopper(2, 1, 0, 0), tmc.DRIVERS['5160']) \
        == pytest.approx(12e6 / (2 * (36 + 12 + 32)))


def test_label_and_snippet():
    combo = tmc.Chopper(1, 5, 4, 4, tpfd=2)
    assert combo.label() == 'tbl1_toff5_hstrt4_hend4_tpfd2'
    snippet = tmc.cfg_snippet(tmc.DRIVERS['5160'], 'stepper_x', combo)
    assert '[tmc5160 stepper_x]' in snippet
    assert 'driver_TOFF: 5' in snippet
    assert 'driver_TPFD: 2' in snippet
    assert 'TPFD' not in tmc.cfg_snippet(tmc.DRIVERS['2209'], 'stepper_x', tmc.Chopper(1, 5, 4, 4))


def test_stock_registers_are_a_driver_property():
    # klippy/extras/tmcXXXX.py program these when the config carries no driver_* lines
    assert tmc.DRIVERS['2209'].default == tmc.Chopper(2, 3, 5, 0)
    assert tmc.DRIVERS['2208'].default == tmc.Chopper(2, 3, 5, 0)
    assert tmc.DRIVERS['2240'].default == tmc.Chopper(2, 3, 5, 2, 4)
    assert tmc.DRIVERS['5160'].default == tmc.Chopper(2, 3, 5, 2, 4)
    assert tmc.DRIVERS['2130'].default == tmc.Chopper(1, 4, 0, 7)
    assert tmc.DRIVERS['2660'].default == tmc.Chopper(2, 4, 3, 3)
    assert tmc.driver_default('tmc2240').tpfd == 4
    assert tmc.driver_default('tmc9999') == tmc.KLIPPER_DEFAULT


def test_baseline_chopper_falls_back_to_the_drivers_defaults():
    stock_2240 = tmc.baseline_chopper({}, default=tmc.DRIVERS['2240'].default)
    assert stock_2240 == tmc.Chopper(2, 3, 5, 2, 4)
    # explicit registers win, a missing tpfd line means Klipper's stock tpfd
    assert tmc.baseline_chopper({'tbl': 0, 'toff': 8}, default=tmc.DRIVERS['2240'].default) \
        == tmc.Chopper(0, 8, 5, 2, 4)
    assert tmc.baseline_chopper({}) == tmc.KLIPPER_DEFAULT


def test_stock_spelling_follows_the_run():
    # a run that does not sweep tpfd spells it None on every candidate; the stock
    # reference must be spelled the same way to be found among the measurements
    assert tmc.stock_chopper(tmc.DRIVERS['2240'], sweep_tpfd=False) == tmc.Chopper(2, 3, 5, 2)
    assert tmc.stock_chopper(tmc.DRIVERS['2240'], sweep_tpfd=True) == tmc.Chopper(2, 3, 5, 2, 4)
    assert tmc.stock_chopper(tmc.DRIVERS['2209'], sweep_tpfd=True) == tmc.KLIPPER_DEFAULT


def test_parse_dump_field_reads_klippers_nonzero_only_format():
    lines = ['GCONF:      00000004 en_spreadcycle=1 pdn_disable=1',
             'CHOPCONF:   331082f1 toff=1 hstrt=7 hend=5 tbl=1 tpfd=1']
    assert tmc.parse_dump_field(lines, 'GCONF', 'en_spreadcycle') == 1
    assert tmc.parse_dump_field(lines, 'CHOPCONF', 'hend') == 5
    # Klipper prints only non-zero fields: a present register without the field = 0
    assert tmc.parse_dump_field(['GCONF:      00000000'], 'GCONF', 'en_spreadcycle') == 0
    assert tmc.parse_dump_field(lines, 'PWMCONF', 'pwm_freq') is None
