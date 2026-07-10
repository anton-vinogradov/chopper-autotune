from chopper_autotune.find_speed import find_valleys
from chopper_autotune.resonance_map import quieter_alternatives, render_map


def test_find_valleys_are_mirrored_peaks():
    # a dip at index 3 (200) between two humps
    mags = [500, 800, 900, 200, 850, 880, 520]
    valleys = find_valleys(mags)
    assert 3 in valleys
    # an endpoint minimum is not a valley (we don't recommend the slowest speed)
    assert 0 not in valleys and len(mags) - 1 not in valleys


def test_quieter_alternatives_flags_a_bump():
    # 200 mm/s sits on a bump (1646); 180 (1266) and 240 (1190) are quieter
    curve = [(180, 1266), (190, 1332), (200, 1646), (210, 1626), (240, 1190)]
    at, below, above = quieter_alternatives(curve, 200)
    assert at == (200, 1646)
    assert below == (180, 1266)          # quietest within the window below
    assert above == (240, 1190)          # quietest within the window above


def test_quieter_alternatives_none_when_already_quiet():
    curve = [(160, 1300), (170, 1287), (180, 1266), (190, 1332), (200, 1646)]
    at, below, above = quieter_alternatives(curve, 180)
    assert at == (180, 1266)
    assert below is None and above is None   # nothing nearby is meaningfully quieter


def test_render_map_flags_peaks_and_valleys():
    curve = [(20, 100), (30, 100), (40, 400), (50, 100), (60, 90)]
    lines = render_map(curve, peaks=[2], valleys=[3])
    row = lambda s: [l for l in lines if l.strip().startswith(s)][0]
    assert 'VFA risk' in row('40') and 'avoid cruising' in row('40')
    assert 'quiet dip' in row('50')
    assert 'VFA' not in row('50') and 'quiet dip' not in row('40')


def test_map_macro_args_translate():
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    parser = build_parser()
    args = parser.parse_args(_gcode_args(
        ['map', 'MOTOR=B', 'MAX_SPEED=300', 'STEP=5', 'PRINT_SPEED=200', 'DRY_RUN=1'],
        boolean_flags(parser)))
    assert (args.axis == 'y' and args.max_speed == 300 and args.step == 5
            and args.print_speed == 200 and args.dry_run)


def test_map_state_round_trips(tmp_path, monkeypatch):
    import json

    from chopper_autotune import resonance_map as map_mod
    monkeypatch.setattr(map_mod, 'STATE', str(tmp_path / 'map.json'))
    map_mod.save_state('A', [140, 210], [170], '200→160/240')
    map_mod.save_state('B', [95], [], None)          # a second motor merges, not replaces
    state = json.load(open(map_mod.STATE))
    assert state['A'] == {'peaks': [140, 210], 'dips': [170], 'advice': '200→160/240'}
    assert state['B'] == {'peaks': [95], 'dips': [], 'advice': None}
