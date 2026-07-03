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


def test_label_and_snippet():
    combo = tmc.Chopper(1, 5, 4, 4, tpfd=2)
    assert combo.label() == 'tbl1_toff5_hstrt4_hend4_tpfd2'
    snippet = tmc.cfg_snippet(tmc.DRIVERS['5160'], 'stepper_x', combo)
    assert '[tmc5160 stepper_x]' in snippet
    assert 'driver_TOFF: 5' in snippet
    assert 'driver_TPFD: 2' in snippet
    assert 'TPFD' not in tmc.cfg_snippet(tmc.DRIVERS['2209'], 'stepper_x', tmc.Chopper(1, 5, 4, 4))
