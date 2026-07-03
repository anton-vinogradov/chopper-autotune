import pytest

from chopper_autotune import tmc
from chopper_autotune.collect import Range, build_plan, measurement_id, steady_window, travel_for


def test_range_parse():
    assert Range.parse('55') == Range(55, 55)
    assert Range.parse('40:70') == Range(40, 70)
    assert list(Range.parse('1:3').values()) == [1, 2, 3]
    with pytest.raises(ValueError):
        Range.parse('7:3')


def test_build_plan_filters_constraints():
    plan = build_plan(tmc.DRIVERS['2209'], Range(0, 3), Range(1, 2), Range(0, 7), Range(0, 15),
                      None, [55])
    assert all(tmc.validate(combo) is None for combo, _ in plan)
    assert all(combo.tpfd is None for combo, _ in plan)
    # 118 valid hstrt/hend pairs (raw sum <= 18); toff=1 allowed only for tbl 2..3
    assert len(plan) == (4 + 2) * 118


def test_build_plan_tpfd_only_when_supported():
    args = (Range(0, 0), Range(3, 3), Range(0, 0), Range(0, 0), Range(0, 1), [55])
    assert {c.tpfd for c, _ in build_plan(tmc.DRIVERS['5160'], *args)} == {0, 1}
    assert {c.tpfd for c, _ in build_plan(tmc.DRIVERS['2209'], *args)} == {None}


def test_build_plan_multiplies_speeds():
    plan = build_plan(tmc.DRIVERS['2209'], Range(0, 0), Range(3, 3), Range(0, 0), Range(0, 0),
                      None, [40, 41, 42])
    assert [speed for _, speed in plan] == [40, 41, 42]


def test_measurement_id_stable():
    assert (measurement_id(tmc.Chopper(0, 3, 5, 7), 55, 0, 1)
            == 'tbl0_toff3_hstrt5_hend7_v55_i0_fwd')
    assert measurement_id(tmc.Chopper(0, 3, 5, 7), 55, 1, -1).endswith('_i1_rev')


def test_travel_for():
    assert travel_for(55, 3025, 1.25) == pytest.approx(55 ** 2 / 3025 + 55 * 1.25)


def test_steady_window_covers_cruise_only():
    # move ends at t=100: decel 55/3025=1/55... use round numbers: v=50, a=1000 -> accel_time 0.05
    start, end = steady_window(100.0, 50, 1000, 1.25, 0.1)
    assert start == pytest.approx(100.0 - 0.05 - 1.25 + 0.125)
    assert end == pytest.approx(100.0 - 0.05 - 0.125)
    assert end - start == pytest.approx(1.25 - 2 * 0.125)
