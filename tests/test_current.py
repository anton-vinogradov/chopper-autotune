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

    def settings(self):
        return {}                                  # no homing_override on this machine


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

def test_unify_recommendation_maxes_coupled_twins_within_each_rating():
    from chopper_autotune.current import unify_recommendation
    rec, cfg = {'x': 0.95, 'y': 0.6}, {'x': 1.0, 'y': 1.0}
    assert unify_recommendation(rec, cfg, coupled=True, per_motor=False) == {'x': 0.95, 'y': 0.95}
    assert unify_recommendation(rec, cfg, coupled=True, per_motor=True) == rec    # opt-out
    assert unify_recommendation(rec, cfg, coupled=False, per_motor=False) == rec  # cartesian differs
    assert unify_recommendation({'x': 0.7}, cfg, coupled=True, per_motor=False) == {'x': 0.7}
    # non-identical motors: the unified value never exceeds a motor's own configured current
    assert unify_recommendation({'x': 1.0, 'y': 0.6}, {'x': 1.5, 'y': 0.8},
                                coupled=True, per_motor=False) == {'x': 1.0, 'y': 0.8}


SENSORLESS = {'stepper_x': {'endstop_pin': 'tmc2209_stepper_x:virtual_endstop',
                            'homing_speed': 20.0, 'position_min': 0.0,
                            'position_endstop': 120, 'position_max': 120}}


def test_make_referee_picks_the_judge_by_endstop_kind():
    from chopper_autotune.current import TimingReferee, make_referee
    assert isinstance(make_referee(None, 'x', SENSORLESS, 60.0), TimingReferee)
    kl = FakeKl()
    assert isinstance(make_referee(kl, 'x', SETTINGS, 60.0), Referee)


class FakeStopwatchKl:
    """print_time pairs: each timed homing consumes two calls, the second advancing
    the clock by the next scripted duration."""

    def __init__(self, durations):
        self.durations = list(durations)
        self.clock = 0.0
        self.calls = 0
        self.scripts = []

    def gcode(self, script):
        self.scripts.append(script)

    def settings(self):
        return {}

    def print_time(self):
        self.calls += 1
        if self.calls % 2 == 0:
            self.clock += self.durations.pop(0)
        return self.clock


def test_timing_referee_reads_slip_from_homing_duration():
    from chopper_autotune.current import TimingReferee
    # warm-up 9.4 (discarded, the first wall seat runs long), then a steady 10.0s bias;
    # the verdict's homings run 0.1s SHORT = the head sits 2mm closer to the wall
    kl = FakeStopwatchKl([9.4, 10.0, 10.0, 10.0, 10.0, 9.9, 9.9])
    ref = TimingReferee(kl, 'x', SENSORLESS, park_other=60.0)
    ref.calibrate()
    assert ref.bias == pytest.approx(10.0)
    assert ref.slipped() == pytest.approx(2.0, abs=1e-6)
    assert any(s.startswith('G28 X') for s in kl.scripts)


def test_timing_referee_refuses_a_noisy_stopwatch():
    from chopper_autotune.current import TimingReferee
    # calibration rituals 0.3s apart = 6mm at homing_speed 20 — beyond TIMING_MAX_SPREAD
    kl = FakeStopwatchKl([9.4, 10.0, 10.0, 10.3, 10.3])
    with pytest.raises(SystemExit, match='too noisy'):
        TimingReferee(kl, 'x', SENSORLESS, park_other=60.0).calibrate()


def test_envelope_still_refuses_sensorless():
    from chopper_autotune.current import sensorless_pin
    assert sensorless_pin(SENSORLESS, 'x') == 'tmc2209_stepper_x:virtual_endstop'
    assert sensorless_pin(SETTINGS, 'x') is None


class FakeOverrideKl:
    """A machine whose homing_override forces Z belief to set_position_z and then
    hops the bed: undo_override_z must walk it back with a relative move."""

    def __init__(self, spz, z_after):
        self.spz = spz
        self.z_after = z_after
        self.scripts = []

    def settings(self):
        return {'homing_override': {'set_position_z': self.spz}}

    def request(self, method, params=None):
        assert method == 'objects/query'
        return {'status': {'toolhead': {'position': [60.0, 60.0, self.z_after, 0.0]}}}

    def gcode(self, script):
        self.scripts.append(script)


def test_undo_override_z_walks_the_bed_back():
    from chopper_autotune.collect import undo_override_z
    kl = FakeOverrideKl(spz=0.0, z_after=5.0)
    undo_override_z(kl)
    assert any('G1 Z-5.000' in s and 'G91' in s for s in kl.scripts)


def test_undo_override_z_skips_a_truly_homed_z():
    from chopper_autotune.collect import undo_override_z
    # belief far from set_position_z = the override actually homed Z; undoing that
    # "shift" would crash the bed, so the guard must not move anything
    kl = FakeOverrideKl(spz=0.0, z_after=119.0)
    undo_override_z(kl)
    assert kl.scripts == []


def test_roar_ratio_gates_on_both_sides():
    import numpy as np

    from chopper_autotune.current import roar_ratio
    t = np.linspace(0, 1, 200)
    samples = np.stack([t, np.sin(40 * t), np.zeros_like(t), np.zeros_like(t)], axis=1)
    assert roar_ratio(None, 100.0) is None
    assert roar_ratio(samples, None) is None
    assert roar_ratio(samples, 100.0) > 0
