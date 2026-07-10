import pytest

from chopper_autotune import tmc
from chopper_autotune.extruder import extruder_context, oscillation, resonant_speed


def test_resonant_speed_picks_the_peak():
    # the measured E0 curve: 5 mm/s rang 3x above the neighbours
    curve = [(1, 886), (2, 872), (3, 733), (5, 2211), (8, 643), (12, 628)]
    assert resonant_speed(curve) == 5


def test_oscillation_is_net_zero():
    script = oscillation(5.0, 2.5, cycles=3)
    lines = script.splitlines()
    assert len(lines) == 6
    forward = sum(1 for l in lines if 'DISTANCE=2.50' in l)
    back = sum(1 for l in lines if 'DISTANCE=-2.50' in l)
    assert forward == back == 3                     # never a net feed or retract
    assert all('STEPPER=extruder' in l for l in lines)


def test_extruder_context_reads_the_tmc_section():
    settings = {
        'tmc2209 extruder': {'driver_tbl': '2', 'driver_toff': '3', 'driver_hstrt': '5',
                             'driver_hend': '0', 'stealthchop_threshold': '0'},
        'extruder': {'min_extrude_temp': '170', 'rotation_distance': '3.5'},
    }
    driver, name, regs, stealth, min_temp = extruder_context(settings)
    assert name == '2209' and driver is tmc.DRIVERS['2209']
    assert regs == {'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 0}
    assert stealth is None and min_temp == 170


def test_extruder_context_requires_a_driver():
    with pytest.raises(SystemExit, match='extruder'):
        extruder_context({'extruder': {}})


def test_extruder_macro_args_translate():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    args = parser.parse_args(_gcode_args(
        ['extruder', 'TEMP=240', 'SPEED=5', 'SAVE=1', 'DRY_RUN=1'], boolean_flags(parser)))
    assert (args.temp == 240 and args.speed == 5 and args.save and args.dry_run
            and args.min_speed == 1 and args.max_speed == 12)
