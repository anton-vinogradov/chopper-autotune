import pytest

from chopper_autotune.analyze import updated_scalars
from chopper_autotune.current import (CREEP_START, Referee, bisect_threshold, referee_axis,
                                      stress_vector)


def test_stress_vector_loads_one_motor():
    # coupled XY: X=Y diagonal is stepper_x alone, X=-Y is stepper_y alone
    assert stress_vector('corexy', 'x') == (1.0, 1.0)
    assert stress_vector('corexy', 'y') == (1.0, -1.0)
    # cartesian: each motor owns its axis
    assert stress_vector('cartesian', 'x') == (1.0, 0.0)
    assert stress_vector('cartesian', 'y') == (0.0, 1.0)


def test_referee_axis():
    assert referee_axis('corexy', 'y') == 'x'      # either endstop sees half the slip
    assert referee_axis('cartesian', 'y') == 'y'
    assert referee_axis('cartesian', 'x') == 'x'


def test_bisect_threshold_converges():
    calls = []

    def holds(current):
        calls.append(current)
        return current >= 0.42

    threshold = bisect_threshold(holds, 0.3, 1.0, 0.05)
    assert 0.42 <= threshold <= 0.47
    assert len(calls) <= 5                         # log2(0.7 / 0.05) rungs


class FakeKl:
    """Endstop triggers after the head physically travels `trigger_after` mm of creep."""

    def __init__(self, trigger_after):
        self.trigger_after = trigger_after
        self.travelled = 0.0
        self.last_target = None
        self.scripts = []

    def gcode(self, script):
        self.scripts.append(script)
        for line in script.splitlines():
            if line.startswith('G1 X') or line.startswith('G1 Y'):
                target = float(line.split()[1][1:])
                if self.last_target is not None:
                    self.travelled += abs(target - self.last_target)
                self.last_target = target
            if line.startswith('SET_KINEMATIC_POSITION'):
                self.last_target = None            # position lie resets tracking
            if line.startswith('G28'):
                self.travelled = 0.0
                self.last_target = None

    def request(self, method):
        assert method == 'query_endstops/status'
        state = 'TRIGGERED' if self.travelled >= self.trigger_after else 'open'
        return {'stepper_x': state, 'stepper_y': 'open'}


SETTINGS = {'stepper_x': {'position_endstop': 260.0, 'position_min': 0.0,
                          'position_max': 260.0}}


def test_referee_measures_offset_and_bias():
    # trigger exactly where expected (CREEP_START of creep) -> offset ~0
    kl = FakeKl(trigger_after=CREEP_START)
    ref = Referee(kl, 'x', SETTINGS, park_other=130.0)
    ref.calibrate()
    assert abs(ref.bias) <= 0.21

    # steps lost TOWARD the endstop: trigger 2mm early -> slipped ~ +2
    kl = FakeKl(trigger_after=CREEP_START - 2.0)
    ref2 = Referee(kl, 'x', SETTINGS, park_other=130.0)
    ref2.bias = ref.bias
    assert ref2.slipped() == pytest.approx(2.0, abs=0.3)

    # slipped beyond the creep range -> None (huge slip)
    kl = FakeKl(trigger_after=1000.0)
    ref3 = Referee(kl, 'x', SETTINGS, park_other=130.0)
    assert ref3.slipped() is None


def test_referee_calibration_rejects_broken_endstop():
    kl = FakeKl(trigger_after=1000.0)
    with pytest.raises(SystemExit, match='calibration failed'):
        Referee(kl, 'x', SETTINGS, park_other=130.0).calibrate()


def test_updated_scalars_replaces_run_current():
    text = ('[tmc2209 stepper_x]\n'
            'uart_pin: PA1\n'
            'run_current: 1.8\n'
            'interpolate: False\n'
            '\n'
            '[tmc2209 stepper_y]\n'
            'run_current: 1.8\n')
    out = updated_scalars(text, 'tmc2209 stepper_x', {'run_current': '1.00'})
    assert 'run_current: 1.00' in out
    assert out.count('run_current: 1.8') == 1      # stepper_y untouched
    assert 'uart_pin: PA1' in out and 'interpolate: False' in out


def test_current_macro_args_translate():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    args = parser.parse_args(_gcode_args(
        ['current', 'MOTOR=A', 'MARGIN=1.5', 'SAVE=1'], boolean_flags(parser)))
    assert args.axis == 'x' and args.margin == 1.5 and args.save