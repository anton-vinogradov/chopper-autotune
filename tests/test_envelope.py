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
