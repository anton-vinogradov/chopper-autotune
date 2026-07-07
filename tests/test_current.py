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


ENDSTOP_POS = 260.0


class FakeKl:
    """Position model of the X gantry + endstop, so the coarse+fine creep (which backs
    off and re-approaches, i.e. non-monotonic) is measured faithfully.

    Tracks the true physical position and Klipper's belief separately: G1 moves both by
    the commanded delta, SET_KINEMATIC_POSITION rewrites the belief without moving the
    head (the lie), G28 snaps both to the endstop. The endstop triggers on the *physical*
    position; `slip` mm of lost steps toward the endstop shifts that trigger point earlier."""

    def __init__(self, slip=0.0):
        self.slip = slip
        self.phys = None
        self.belief = None
        self.scripts = []

    def gcode(self, script):
        self.scripts.append(script)
        for line in script.splitlines():
            if line.startswith('G1 X'):
                target = float(line.split()[1][1:])
                if self.phys is None:
                    self.phys = target             # first move: head is where it goes
                elif self.belief is not None:
                    self.phys += target - self.belief
                self.belief = target
            elif line.startswith('SET_KINEMATIC_POSITION'):
                self.belief = float(line.split('X=')[1].split()[0])
            elif line.startswith('G28'):
                self.phys = self.belief = ENDSTOP_POS

    def request(self, method):
        assert method == 'query_endstops/status'
        triggered = self.phys is not None and self.phys >= ENDSTOP_POS - self.slip
        return {'stepper_x': 'TRIGGERED' if triggered else 'open', 'stepper_y': 'open'}


SETTINGS = {'stepper_x': {'position_endstop': ENDSTOP_POS, 'position_min': 0.0,
                          'position_max': ENDSTOP_POS}}


def test_referee_measures_offset_and_bias():
    # no lost steps: trigger exactly where expected -> offset ~0 (fine-creep resolution)
    kl = FakeKl(slip=0.0)
    ref = Referee(kl, 'x', SETTINGS, park_other=130.0)
    ref.calibrate()
    assert abs(ref.bias) <= 0.21

    # steps lost TOWARD the endstop: trigger 2mm early -> slipped ~ +2
    kl = FakeKl(slip=2.0)
    ref2 = Referee(kl, 'x', SETTINGS, park_other=130.0)
    ref2.bias = ref.bias
    assert ref2.slipped() == pytest.approx(2.0, abs=0.25)

    # steps lost AWAY from the endstop: trigger late -> slipped ~ -3
    kl = FakeKl(slip=-3.0)
    ref3 = Referee(kl, 'x', SETTINGS, park_other=130.0)
    ref3.bias = ref.bias
    assert ref3.slipped() == pytest.approx(-3.0, abs=0.25)

    # slipped beyond the creep range -> None (huge slip)
    kl = FakeKl(slip=-1000.0)
    ref4 = Referee(kl, 'x', SETTINGS, park_other=130.0)
    assert ref4.slipped() is None


def test_referee_calibration_rejects_broken_endstop():
    kl = FakeKl(slip=-1000.0)
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