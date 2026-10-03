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

    def __init__(self, slip=0.0, names=('stepper_x', 'stepper_y')):
        self.slip = slip
        self.names = names
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

    def settings(self):
        return {}                   # no G28 override with a z_hop

    def info(self):
        return {}                   # Klipper path unknown: no SET_HOMED

    def request(self, method):
        assert method == 'query_endstops/status'
        triggered = self.phys is not None and self.phys >= ENDSTOP_POS - self.slip
        return {self.names[0]: 'TRIGGERED' if triggered else 'open', self.names[1]: 'open'}


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


@pytest.mark.parametrize('names', [('x', 'y'), ('stepper_x', 'stepper_y')])
def test_referee_reads_the_endstop_by_either_name(names):
    # every release and Kalico name it by the rail ('x'), Klipper master since May 2025
    # by the section: an unread trigger crept the head 16 mm past the switch
    kl = FakeKl(names=names)
    ref = Referee(kl, 'x', SETTINGS, park_other=130.0)
    ref.calibrate()
    assert abs(ref.bias) <= 0.21


def test_referee_moves_nothing_without_an_endstop_it_can_read():
    kl = FakeKl(names=('probe', 'z'))
    with pytest.raises(SystemExit, match='reports no endstop for x'):
        Referee(kl, 'x', SETTINGS, park_other=130.0).calibrate()
    assert kl.scripts == []


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


def test_referee_refuses_sensorless_endstops():
    import pytest

    from chopper_autotune.current import Referee
    settings = {'stepper_x': {'endstop_pin': 'tmc2209_stepper_x:virtual_endstop',
                              'position_endstop': 120, 'position_max': 120}}
    with pytest.raises(SystemExit, match='sensorless'):
        Referee(None, 'x', settings, 60.0)


def test_a_stroke_peaks_where_it_brakes_as_hard_as_it_accelerates():
    from chopper_autotune.current import stroke_accel, stroke_peak
    assert stroke_peak(25.0, (1.0, 0.0), 3000) == pytest.approx(387.3, abs=0.1)
    assert stroke_peak(25.0, (1.0, 0.0), 500) == pytest.approx(158.1, abs=0.1)
    # corexy: the head runs 1/sqrt2 of the belt speed along a 2*span*sqrt2 diagonal
    assert stroke_peak(25.0, (1.0, 1.0), 500) == pytest.approx(265.9, abs=0.1)
    assert stroke_accel(200, 25.0, (1.0, 0.0)) == 800           # 200^2 / 50
    assert stroke_peak(25.0, (1.0, 0.0), stroke_accel(200, 25.0, (1.0, 0.0))) >= 200


def status(max_velocity=300.0, ratio=0.5, speed_factor=1.0):
    """objects/query of the live limits, as Klipper answers it."""
    return lambda method, params: {'status': {
        'toolhead': {'max_velocity': max_velocity, 'max_accel': 3000.0, 'minimum_cruise_ratio': ratio},
        'gcode_move': {'speed_factor': speed_factor}}}


@pytest.mark.parametrize('max_accel, max_velocity, refusal', [
    # a 50 mm stroke at 500 mm/s2 peaks at 158 mm/s: the 200 mm/s rung would be a weaker
    # load than the header says, and the saved current too low
    (500.0, 300.0, 'raise ACCEL to 800 or more: at 500 a stroke of motor A peaks at 158 of the 200'),
    # G1 feed never passes max_velocity (set at runtime too)
    (3000.0, 160.0, 'raise max_velocity to 200 or more: now 160, it caps motor A at 160 of the 200'),
])
def test_the_pattern_refuses_what_its_strokes_cannot_carry(monkeypatch, max_accel, max_velocity,
                                                           refusal):
    from types import SimpleNamespace

    import chopper_autotune.current as cur
    from chopper_autotune.cli import build_parser
    hw = SimpleNamespace(kinematics='cartesian', axis_span=300.0, max_accel=max_accel,
                         driver=SimpleNamespace(name='2209'))
    monkeypatch.setattr(cur, 'detect_hardware', lambda kl, axis, accel=False: hw)
    scripts = []
    kl = SimpleNamespace(gcode=scripts.append, request=status(max_velocity), settings=lambda: {
        'tmc2209 stepper_x': {'run_current': 0.8}, 'stepper_x': {}})
    with pytest.raises(SystemExit) as refused:
        cur.current_tune(kl, build_parser().parse_args(['current', '--motor', 'a', '--yes']))
    assert str(refused.value.code).startswith(refusal) and scripts == []
    # the action reaches the display: its first words on a 16-character LCD row
    from chopper_autotune.collect import failure_display
    assert failure_display('current FAILED: %s' % refused.value.code)[:16].startswith('FAIL raise ')


@pytest.mark.parametrize('settings, answers, freed, restored', [
    ({}, [], ['SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0'],
     ['SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0.35']),
    # a print left M220 S80, and an input shaper smooths what the motors get
    ({'input_shaper': {}}, ['// shaper_type_x:mzv shaper_freq_x:40.000 damping_ratio_x:0.100000',
                            '// shaper_type_y:ei shaper_freq_y:35.500 damping_ratio_y:0.100000'],
     ['SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0', 'M220 S100',
      'SET_INPUT_SHAPER SHAPER_FREQ_X=0 SHAPER_FREQ_Y=0'],
     ['SET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0.35', 'M220 S80',
      'SET_INPUT_SHAPER SHAPER_FREQ_X=40.000 SHAPER_FREQ_Y=35.500']),
])
def test_the_strokes_run_free_and_everything_comes_back(settings, answers, freed, restored):
    from types import SimpleNamespace

    from chopper_autotune.current import free_strokes, live_limits
    scripts = []
    kl = SimpleNamespace(gcode=scripts.append, gcode_output=lambda script: answers,
                         request=status(ratio=0.35, speed_factor=0.8 if answers else 1.0))
    restores = []
    free_strokes(kl, settings, live_limits(kl), restores)
    assert scripts == freed
    del scripts[:]
    for step in restores:
        step()
    assert scripts == restored


def test_a_stop_during_the_first_change_still_puts_it_back():
    # the way back is registered before the command: a Stop may land while it runs
    from types import SimpleNamespace

    from chopper_autotune.current import free_strokes, live_limits

    def gcode(script):
        raise SystemExit(143)
    kl = SimpleNamespace(gcode=gcode, request=status())
    restores = []
    with pytest.raises(SystemExit):
        free_strokes(kl, {}, live_limits(kl), restores)
    assert len(restores) == 1


KINEMATICS_REPORTS = {
    # SET_KINEMATICS_LIMIT without parameters, as Kalico's limited_* kinematics answer it
    'limited_corexy': ['// x,y,z max_accels: (3000.0, 2000.0, 100.0)\n'
                       '// Per axis accelerations limits scale with current acceleration.\n'
                       '// Minimum XY acceleration of 1664 mm/s\u00b2 reached on 56\u00b0 diagonals.'],
    'limited_cartesian': ['// x,y,z max_velocities: [300.0, 200.0, 15.0]\n'
                          '// x,y,z max_accels: [3000.0, 2000.0, 100.0]\n'
                          '// Per axis accelerations limits are independent of current acceleration.'],
}


@pytest.mark.parametrize('kinematics, motor, lifted, restored', [
    # the strokes run diagonals: both axes
    ('limited_corexy', 'y', 'SET_KINEMATICS_LIMIT SCALE=0 X_ACCEL=1000000 Y_ACCEL=1000000',
     'SET_KINEMATICS_LIMIT SCALE=1 X_ACCEL=3000.0 Y_ACCEL=2000.0'),
    # the motor's own axis: the other keeps its limit for the moves that set the strokes up
    ('limited_cartesian', 'x', 'SET_KINEMATICS_LIMIT SCALE=0 X_ACCEL=1000000 Y_ACCEL=2000.0',
     'SET_KINEMATICS_LIMIT SCALE=0 X_ACCEL=3000.0 Y_ACCEL=2000.0'),
    ('limited_cartesian', 'y', 'SET_KINEMATICS_LIMIT SCALE=0 X_ACCEL=3000.0 Y_ACCEL=1000000',
     'SET_KINEMATICS_LIMIT SCALE=0 X_ACCEL=3000.0 Y_ACCEL=2000.0'),
])
def test_kalicos_accel_limits_step_aside_for_a_motors_strokes_and_come_back(kinematics, motor,
                                                                           lifted, restored):
    from types import SimpleNamespace

    from chopper_autotune.current import axis_limits, keep_axis_limits, lift_axis_limits
    scripts = []
    kl = SimpleNamespace(gcode=scripts.append,
                         gcode_output=lambda script: KINEMATICS_REPORTS[kinematics])
    limits, restores = axis_limits(kl, kinematics), []
    keep_axis_limits(kl, limits, restores)
    lift_axis_limits(kl, kinematics, limits, motor)
    assert scripts == [lifted]
    restores[0]()
    assert scripts == [lifted, restored]
    keep_axis_limits(kl, None, restores)
    lift_axis_limits(kl, 'corexy', None, motor)
    assert len(restores) == 1 and scripts == [lifted, restored]
    assert axis_limits(kl, 'corexy') is None
    with pytest.raises(SystemExit, match='did not report the limited_corexy limits'):
        axis_limits(SimpleNamespace(gcode_output=lambda script: ['// Unknown command']),
                    'limited_corexy')


@pytest.mark.parametrize('kinematics, vec, accel, scale, expected', [
    ('corexy', (1.0, 1.0), 10000.0, False, 10000.0),
    # a diagonal of motor A: sqrt2 / (1/3000 + 1/2000)
    ('limited_corexy', (1.0, 1.0), 10000.0, False, 1697.06),
    ('limited_corexy', (1.0, 1.0), 1000.0, False, 1000.0),
    # scale_xy_accel: the cap follows M204 over the larger limit
    ('limited_corexy', (1.0, 1.0), 10000.0, True, 5656.85),
    ('limited_cartesian', (0.0, 1.0), 10000.0, False, 2000.0),
    ('limited_cartesian', (1.0, 0.0), 2500.0, False, 2500.0),
    ('limited_cartesian', (1.0, 0.0), 10000.0, True, 8320.5),
])
def test_the_accel_a_stroke_gets_follows_kalicos_per_axis_limits(kinematics, vec, accel, scale,
                                                                 expected):
    from chopper_autotune.current import accel_along
    limits = {'accels': [3000.0, 2000.0], 'velocities': [], 'scale': scale}
    assert accel_along(kinematics, vec, accel, limits if kinematics.startswith('limited') else None) \
        == pytest.approx(expected, abs=0.01)


@pytest.mark.parametrize('kinematics, max_velocity, refusal', [
    # limited_corexy caps the belt itself: 180 lets no belt reach 200
    ('limited_corexy', 180.0, 'raise max_velocity to 200 or more: now 180, it caps motor A at 180'),
    # Klipper caps the head: on corexy 180 lets a belt reach 254
    ('corexy', 180.0, None),
    ('limited_corexy', 200.0, None),
])
def test_a_limited_corexy_belt_is_capped_by_max_velocity_itself(monkeypatch, kinematics,
                                                               max_velocity, refusal):
    from types import SimpleNamespace

    import chopper_autotune.current as cur
    from chopper_autotune.cli import build_parser
    hw = SimpleNamespace(kinematics=kinematics, axis_span=300.0, max_accel=10000.0,
                         driver=SimpleNamespace(name='2209'))
    monkeypatch.setattr(cur, 'detect_hardware', lambda kl, axis, accel=False: hw)
    scripts = []
    kl = SimpleNamespace(gcode=scripts.append, request=status(max_velocity),
                         gcode_output=lambda script: KINEMATICS_REPORTS['limited_corexy'],
                         settings=lambda: {'tmc2209 stepper_x': {'run_current': 0.8}, 'stepper_x': {}})
    args = build_parser().parse_args(['current', '--motor', 'a', '--yes', '--dry-run'])
    if refusal:
        with pytest.raises(SystemExit) as refused:
            cur.current_tune(kl, args)
        assert str(refused.value.code).startswith(refusal)
    else:
        assert cur.current_tune(kl, args) == 0
    assert scripts == []


def limited_current(monkeypatch, kinematics, report, max_velocity=500.0):
    from types import SimpleNamespace

    import chopper_autotune.current as cur
    hw = SimpleNamespace(kinematics=kinematics, axis_span=300.0, max_accel=10000.0,
                         driver=SimpleNamespace(name='2209'))
    monkeypatch.setattr(cur, 'detect_hardware', lambda kl, axis, accel=False: hw)
    return SimpleNamespace(gcode=lambda script: pytest.fail('moved: %s' % script),
                           request=status(max_velocity), gcode_output=lambda script: report,
                           settings=lambda: {'tmc2209 stepper_%s' % m: {'run_current': 0.8}
                                             for m in 'xy'})


@pytest.mark.parametrize('kinematics, motor, accel, line', [
    # the printer's own load: what its per-axis limits give the stroke
    ('limited_cartesian', 'b', None, '  motor B: configured run_current 0.80 A, strokes at 2000 '
                                     'mm/s2 (what the per-axis limits give it of max_accel 10000)'),
    ('limited_corexy', 'a', None, '  motor A: configured run_current 0.80 A, strokes at 5657 '
                                  'mm/s2 (what the per-axis limits give it of max_accel 10000)'),
    # an ACCEL given runs as asked: the strokes lift the limit
    ('limited_cartesian', 'b', '4000', '  motor B: configured run_current 0.80 A, strokes at 4000 mm/s2'),
    ('corexy', 'a', None, '  motor A: configured run_current 0.80 A, strokes at 10000 mm/s2'),
])
def test_current_strokes_load_a_motor_as_the_printer_does(monkeypatch, capsys, kinematics, motor,
                                                         accel, line):
    # Kalico caps each axis: max_accel alone loaded a heavy bed 3x past what it ever gets
    import chopper_autotune.current as cur
    from chopper_autotune.cli import build_parser
    kl = limited_current(monkeypatch, kinematics, KINEMATICS_REPORTS.get(kinematics, []))
    cur.current_tune(kl, build_parser().parse_args(
        ['current', '--motor', motor, '--dry-run'] + (['--accel', accel] if accel else [])))
    assert line in capsys.readouterr().out.splitlines()


def test_current_refuses_a_belt_kalico_caps_below_the_pattern(monkeypatch):
    # max_y_velocity is the user's limit for the Y motor, as max_velocity is the head's
    import chopper_autotune.current as cur
    from chopper_autotune.cli import build_parser
    report = ['// x,y,z max_velocities: [300.0, 150.0, 15.0]\n'
              '// x,y,z max_accels: [3000.0, 2000.0, 100.0]\n'
              '// Per axis accelerations limits are independent of current acceleration.']
    for max_velocity in (500.0, 200.0):             # at 200 max_velocity is no cap to raise
        kl = limited_current(monkeypatch, 'limited_cartesian', report, max_velocity)
        with pytest.raises(SystemExit) as refused:
            cur.current_tune(kl, build_parser().parse_args(['current', '--motor', 'b', '--dry-run']))
        assert str(refused.value.code).startswith(
            'raise max_y_velocity to 200 or more: now 150, it caps motor B at 150 of the 200 mm/s')
        assert cur.current_tune(kl, build_parser().parse_args(
            ['current', '--motor', 'a', '--dry-run'])) == 0


def test_a_rung_sets_and_puts_back_the_exact_current():
    # 0.566 A sent as 0.57 set IRUN 18 instead of 17 until Klipper restarted
    from types import SimpleNamespace

    import chopper_autotune.current as cur
    scripts = []
    kl = SimpleNamespace(gcode=scripts.append)
    board = SimpleNamespace(center=(150.0, 150.0))
    original = cur.home_xy
    cur.home_xy = lambda kl_, script: kl_.gcode(script)
    try:
        cur.run_rung(kl, board, 'x', 0.4375, 0.566, (1.0, 0.0), 25.0, 3000.0)
    finally:
        cur.home_xy = original
    currents = [script for script in scripts if script.startswith('SET_TMC_CURRENT')]
    assert currents == ['SET_TMC_CURRENT STEPPER=stepper_x CURRENT=0.4375',
                        'SET_TMC_CURRENT STEPPER=stepper_x CURRENT=0.566']

