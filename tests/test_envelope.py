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


def test_recommend_limits_coupled_xy_and_shaper_cap():
    from chopper_autotune.envelope import recommend_limits
    rec = recommend_limits({'A': 350, 'B': 350}, {'A': 40000, 'B': 40000}, coupled=True,
                           shaper={'x': ('ei', 106.8, 20770), 'y': ('mzv', 50.0, 7365)})
    assert rec['max_velocity_axis'] == 269          # 350 / 1.3: both belts run at head speed
    assert rec['max_velocity'] == 190               # /sqrt(2): a 45deg diagonal runs one belt faster
    assert rec['max_accel'] == 7300                 # the Y shaper wins over motors (30769) and X
    assert 'Y shaper' in rec['limited_by']


def test_recommend_limits_without_shaper_and_cartesian():
    from chopper_autotune.envelope import recommend_limits
    rec = recommend_limits({'A': 200}, {'A': 13000}, coupled=False, shaper={})
    assert rec['max_velocity'] == rec['max_velocity_axis'] == 153
    assert rec['max_accel'] == 10000 and rec['limited_by'] == 'motor torque'


def test_recommend_limits_refuses_a_first_rung_skip():
    from chopper_autotune.envelope import recommend_limits
    assert recommend_limits({'A': None, 'B': 350}, {'A': 40000, 'B': 40000},
                            coupled=True, shaper={}) is None


def test_shaper_accels_absent_klipper_is_quiet(monkeypatch):
    from chopper_autotune import envelope as envelope_mod
    monkeypatch.setattr(envelope_mod, 'KLIPPY_DIR', '/nonexistent')
    assert envelope_mod.shaper_accels({'input_shaper': {'shaper_type_x': 'ei'}}) == {}
