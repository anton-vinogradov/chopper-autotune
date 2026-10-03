import pytest

from chopper_autotune.envelope import MARGIN, ceiling, verdict


def test_ceiling_stops_at_first_skip():
    reported = []
    # skips at 250: the safe ceiling is the rung below it
    hold, skip = ceiling((150, 200, 250, 300),
                         lambda v: v >= 250,
                         lambda v, s: reported.append((v, s)))
    assert (hold, skip) == (200, 250)
    assert reported == [(150, False), (200, False), (250, True)]  # stops after the skip


def test_ceiling_never_skips():
    hold, skip = ceiling((150, 200, 250), lambda v: False, lambda v, s: None)
    assert (hold, skip) == (250, None)


def test_ceiling_skips_immediately():
    hold, skip = ceiling((150, 200), lambda v: True, lambda v, s: None)
    assert (hold, skip) == (None, 150)


def test_verdict_phrasing():
    assert 'not the limit' in verdict(350, None, 'mm/s')          # never skipped
    assert 'margin is too thin' in verdict(None, 150, 'mm/s')     # skips at the floor
    v = verdict(200, 250, 'mm/s')                                 # holds to 200, skips at 250
    assert 'holds to 200' in v and 'skips at 250' in v
    assert '%g' % (200 / MARGIN) in v                             # recommends the derated ceiling


def test_envelope_macro_args_translate():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    args = parser.parse_args(_gcode_args(
        ['envelope', 'MOTOR=B', 'MAX_SPEED=300', 'STEP=25', 'DRY_RUN=1'],
        boolean_flags(parser)))
    assert args.axis == 'y' and args.max_speed == 300 and args.step == 25 and args.dry_run


def test_ceiling_label_shapes():
    from chopper_autotune.envelope import ceiling_label
    assert ceiling_label(350, None) == '350+'          # held the whole range
    assert ceiling_label(300, 350) == '300'            # last safe rung before a skip
    assert ceiling_label(None, 150) == '<150'          # skipped at the first rung
    assert ceiling_label(40000, None, kilo=True) == '40k+'
    assert ceiling_label(30000, 40000, kilo=True) == '30k'


def test_envelope_state_round_trips(tmp_path, monkeypatch):
    import json

    from chopper_autotune import envelope as envelope_mod
    monkeypatch.setattr(envelope_mod, 'STATE', str(tmp_path / 'envelope.json'))
    achieved = {'A': {'speed': '350+', 'accel': '40k+'},
                'B': {'speed': '300', 'accel': '40k+'}}
    envelope_mod.save_state(achieved)
    assert json.load(open(envelope_mod.STATE)) == achieved


def test_envelope_state_merges_per_motor(tmp_path, monkeypatch):
    import json

    from chopper_autotune import envelope as envelope_mod
    monkeypatch.setattr(envelope_mod, 'STATE', str(tmp_path / 'envelope.json'))
    envelope_mod.save_state({'A': {'speed': '350+', 'accel': '40k+'}})
    envelope_mod.save_state({'B': {'speed': '300', 'accel': '30k'}})   # MOTOR=B alone
    saved = json.load(open(envelope_mod.STATE))
    assert saved['A'] == {'speed': '350+', 'accel': '40k+'}            # A survives
    assert saved['B'] == {'speed': '300', 'accel': '30k'}


def test_recommend_limits_separates_where_the_numbers_go():
    from chopper_autotune.envelope import recommend_limits
    rec = recommend_limits({'A': 350, 'B': 350}, {'A': 40000, 'B': 40000}, kinematics='corexy',
                           shaper={'x': ('ei', 106.8, 20770, 8600), 'y': ('mzv', 50.0, 7365, 3100)},
                           printer_now={'max_velocity': 500, 'max_accel': 10000})
    assert rec['max_velocity'] == 247               # tested ceiling / sqrt(2): a 45deg move
    assert rec['max_velocity_margin'] == 190        # runs one belt faster than the head
    assert rec['max_accel'] == 30700                # the MACHINE cap is motor torque /1.3
    assert rec['print_accel'] == 7300               # the Y shaper is print-quality guidance
    assert rec['print_accel_crisp'] == 3100         # ...with the crisp-detail variant alongside
    assert 'Y shaper' in rec['limited_by']
    assert rec['now_velocity'] == 500               # the run can say 'now 500 - over'


def test_recommend_limits_without_shaper_and_cartesian():
    from chopper_autotune.envelope import recommend_limits
    rec = recommend_limits({'A': 200}, {'A': 13000}, kinematics='cartesian', shaper={},
                           printer_now={})
    assert rec['max_velocity'] == 200               # no sqrt(2) coupling on cartesian
    assert rec['max_accel'] == 10000
    assert rec['print_accel'] is None and rec['limited_by'] is None
    assert rec['print_accel_crisp'] is None


def test_recommend_limits_refuses_a_first_rung_skip():
    from chopper_autotune.envelope import recommend_limits
    assert recommend_limits({'A': None, 'B': 350}, {'A': 40000, 'B': 40000},
                            kinematics='corexy', shaper={}, printer_now={}) is None


def test_verdict_now_flags_an_over_limit_config():
    from chopper_autotune.envelope import verdict_now
    assert 'ok' in verdict_now(200, 247, over='outrun')
    assert 'outrun' in verdict_now(500, 247, over='outrun')
    assert verdict_now(None, 247, over='outrun') == ''


def test_shaper_accels_absent_klipper_is_quiet(monkeypatch):
    from chopper_autotune import envelope as envelope_mod
    monkeypatch.setattr(envelope_mod, 'KLIPPY_DIR', '/nonexistent')
    assert envelope_mod.shaper_accels({'input_shaper': {'shaper_type_x': 'ei'}}) == {}


def test_on_limited_corexy_max_velocity_caps_the_belt_in_the_ladders_and_the_advice():
    # Kalico's limited_corexy caps the belt itself, not the head: no sqrt2 either way
    from chopper_autotune.envelope import recommend_limits, stroke_ladders
    assert stroke_ladders('corexy', 'x', 400.0, (250, 300), 3000, (3000,), 100, 200)[2] == (250,)
    _, _, speeds, _, short = stroke_ladders('limited_corexy', 'x', 400.0, (150, 200, 250), 3000,
                                            (3000,), 100, 200)
    assert speeds == (150, 200) and 'raise max_velocity to 250 to reach 250' in short
    held = ({'A': 350, 'B': 350}, {'A': 40000, 'B': 40000})
    assert recommend_limits(*held, kinematics='corexy', shaper={}, printer_now={})['max_velocity'] == 247
    assert recommend_limits(*held, kinematics='limited_corexy', shaper={},
                            printer_now={})['max_velocity'] == 350


def test_on_limited_cartesian_the_advice_names_each_axis_and_pairs_the_head_limits():
    # Kalico refuses a max_x_velocity above max_velocity: min(belts) as max_velocity left
    # the printer unable to start
    from chopper_autotune.envelope import recommend_limits
    held = ({'A': 400, 'B': 250}, {'A': 13000, 'B': 6500})
    rec = recommend_limits(*held, kinematics='limited_cartesian', shaper={}, printer_now={})
    assert rec['per_axis'] == {'max_x_velocity': 400, 'max_y_velocity': 250,
                               'max_x_accel': 10000, 'max_y_accel': 5000}
    assert (rec['max_velocity'], rec['max_accel']) == (471, 11100)    # the hypotenuses
    rec = recommend_limits(*held, kinematics='cartesian', shaper={}, printer_now={})
    assert rec['per_axis'] is None and (rec['max_velocity'], rec['max_accel']) == (250, 5000)


def test_the_ladders_drop_the_rungs_a_stroke_cannot_reach(capsys):
    # a rung never reached would 'hold' and top the max_velocity advice
    import pytest

    from chopper_autotune.envelope import stroke_ladders
    span, vec, speeds, accels, short = stroke_ladders(
        'cartesian', 'x', 400.0, (150, 200, 250, 300), 1000, (500, 1000, 2000), 180, 500)
    assert (span, vec, speeds, accels) == (25.0, (1.0, 0.0), (150, 200), (1000, 2000))
    assert short == ('the strokes stop at 223 mm/s (max_velocity 500, accel 1000): raise ACCEL '
                     'to 1800 to reach 300')
    out = capsys.readouterr().out
    assert 'the speed ladder stops at 200 mm/s' in out and 'accel ladder starts at 1000' in out
    # max_velocity caps the head: a corexy belt runs sqrt2 times as fast
    assert stroke_ladders('corexy', 'x', 400.0, (250, 300), 3000, (3000,), 100, 200)[2] == (250,)
    # every limit in the way is named, and where a max_velocity set at runtime comes from
    _, _, speeds, _, short = stroke_ladders('cartesian', 'x', 400.0, (150, 200, 300), 1500,
                                            (3000,), 100, 160, 300)
    assert speeds == (150,) and short == (
        'the strokes stop at 160 mm/s (max_velocity 160 (set at runtime; printer.cfg has 300), '
        'accel 1500): raise max_velocity to 300 and ACCEL to 1800 to reach 300')
    # the action first: the display shows its first characters (failure_display)
    with pytest.raises(SystemExit, match='^raise ACCEL to 500 or more: motor A runs under 142'):
        stroke_ladders('cartesian', 'x', 400.0, (150, 200), 400, (400,), 100, 500)
    with pytest.raises(SystemExit, match='^raise max_velocity to 150 or more: motor A runs under 121'):
        stroke_ladders('cartesian', 'x', 400.0, (150, 200), 3000, (3000,), 100, 120)
    with pytest.raises(SystemExit, match='^lower ACCEL_PROBE_SPEED to 122 or less'):
        stroke_ladders('cartesian', 'x', 400.0, (100,), 3000, (300,), 150, 500)
    with pytest.raises(SystemExit, match='^lower ACCEL_PROBE_SPEED to 120 or less'):
        stroke_ladders('cartesian', 'x', 400.0, (100,), 3000, (3000,), 150, 120)



@pytest.mark.parametrize('kinematics', ['corexz', 'limited_corexz'])
def test_the_envelope_refuses_corexz_before_asking_klipper_anything(monkeypatch, kinematics):
    # a slipped X motor moves Z too; Kalico's limited_corexz answers SET_KINEMATICS_LIMIT
    # with limits the tool would misread as missing
    from types import SimpleNamespace

    import chopper_autotune.envelope as env
    from chopper_autotune.cli import build_parser
    asked = []
    kl = SimpleNamespace(settings=lambda: {'printer': {'kinematics': kinematics}},
                         gcode=asked.append, gcode_output=lambda script: asked.append(script) or [],
                         request=lambda method, params=None: asked.append(method) or {})
    monkeypatch.setattr(env, 'detect_hardware',
                        lambda kl_, axis, accel=False: SimpleNamespace(kinematics=kinematics))
    with pytest.raises(SystemExit, match='%s: the X motors move Z too' % kinematics):
        env.envelope(kl, build_parser().parse_args(['envelope', '--dry-run']))
    assert asked == []


def test_an_accel_given_runs_the_ladder_as_asked_without_a_per_axis_note(monkeypatch, capsys):
    from types import SimpleNamespace

    import chopper_autotune.envelope as env
    from chopper_autotune.cli import build_parser
    report = ['// x,y,z max_accels: (8000.0, 2000.0, 100.0)\n'
              '// Per axis accelerations limits are independent of current acceleration.']
    kl = SimpleNamespace(settings=lambda: {'printer': {'kinematics': 'limited_corexy'}},
                         gcode_output=lambda script: report,
                         request=lambda method, params=None: {'status': {
                             'toolhead': {'max_velocity': 500.0, 'max_accel': 3000.0, 'minimum_cruise_ratio': 0.5},
                             'gcode_move': {'speed_factor': 1.0}}})
    monkeypatch.setattr(env, 'detect_hardware', lambda kl_, axis, accel=False: SimpleNamespace(
        kinematics='limited_corexy', axis_span=300.0, max_accel=10000.0))
    env.envelope(kl, build_parser().parse_args(['envelope', '--motor', 'a', '--accel', '3000',
                                                '--dry-run']))
    assert 'mm/s (accel 3000); accel ladder 3000/4500/6000/9000/12000' in capsys.readouterr().out
