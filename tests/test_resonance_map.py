from chopper_autotune.resonance_map import quiet_band, render_map


def test_quiet_band_picks_widest_low_span():
    # floor 100 at 30/40/50; a spike at 60; a second low run 80/90 (narrower)
    curve = [(20, 200), (30, 100), (40, 110), (50, 120), (60, 400), (70, 300), (80, 105), (90, 118)]
    # ratio 1.25 -> threshold 125: 30..50 (three points) beats 80..90 (two)
    assert quiet_band(curve, ratio=1.25) == (30, 50)


def test_quiet_band_single_point_and_empty():
    assert quiet_band([(50, 900)]) == (50, 50)
    assert quiet_band([]) is None


def test_render_map_flags_peaks_and_quiet():
    curve = [(20, 100), (30, 100), (40, 400), (50, 100)]
    peaks = [2]                                        # 40 mm/s is the resonance
    lines = render_map(curve, peaks, quiet=(20, 30))
    body = '\n'.join(lines)
    assert 'resonance (avoid cruising here)' in [l for l in lines if l.strip().startswith('40')][0]
    # a quiet-band speed is tagged, the peak is not double-tagged as quiet
    assert 'quiet' in [l for l in lines if l.strip().startswith('20')][0]
    assert 'quiet' not in [l for l in lines if l.strip().startswith('40')][0]
    assert body.count('resonance (avoid cruising here)') == 1


def test_map_macro_args_translate():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    args = parser.parse_args(_gcode_args(
        ['map', 'MOTOR=B', 'MAX_SPEED=300', 'STEP=5', 'DRY_RUN=1'], boolean_flags(parser)))
    assert args.axis == 'y' and args.max_speed == 300 and args.step == 5 and args.dry_run
