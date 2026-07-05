import signal

import pytest

from chopper_autotune.analyze import latest_dataset
from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser, install_sigterm_handler
from chopper_autotune.dataset import Dataset

FLAGS = boolean_flags(build_parser())


def test_gcode_args_translation():
    assert _gcode_args(['collect', 'AXIS=X', 'SPEED=55', 'MEASURE_TIME=1.5'], FLAGS) \
        == ['collect', '--axis', 'X', '--speed', '55', '--measure-time', '1.5']
    assert _gcode_args(['collect', 'TOFF=1:8'], FLAGS) == ['collect', '--toff', '1:8']


def test_gcode_args_flags():
    assert _gcode_args(['collect', 'DRY_RUN=1'], FLAGS) == ['collect', '--dry-run']
    assert _gcode_args(['collect', 'DRY_RUN=0'], FLAGS) == ['collect']
    assert _gcode_args(['analyze', 'APPLY=true', 'TOP=20'], FLAGS) \
        == ['analyze', '--apply', '--top', '20']
    assert _gcode_args(['collect', 'DRY_RUN=on'], FLAGS) == ['collect', '--dry-run']
    assert _gcode_args(['collect', 'DRY_RUN=Y'], FLAGS) == ['collect', '--dry-run']
    assert _gcode_args(['collect', 'DRY_RUN=off'], FLAGS) == ['collect']


def test_gcode_args_rejects_unknown_boolean_value():
    # DRY_RUN=<typo> silently treated as "off" would physically move the printer
    with pytest.raises(SystemExit, match='DRY_RUN'):
        _gcode_args(['collect', 'DRY_RUN=maybe'], FLAGS)


def test_gcode_args_passthrough():
    args = ['analyze', 'datasets/20260703_x', '--top', '5']
    assert _gcode_args(args, FLAGS) == args


def test_boolean_flags_derived_from_parser():
    assert {'--dry-run', '--yes', '--csv', '--no-raw', '--skip-audible',
            '--recompute', '--no-html', '--apply', '--save'} <= FLAGS
    assert '--top' not in FLAGS


def test_dataset_macro_param_parses():
    parser = build_parser()
    args = parser.parse_args(_gcode_args(['analyze', 'DATASET=/tmp/x', 'NO_HTML=1'], FLAGS))
    assert args.dataset_opt == '/tmp/x'
    args = parser.parse_args(_gcode_args(['status', 'DATASET=/tmp/y'], FLAGS))
    assert args.dataset_opt == '/tmp/y'


def test_motor_alias_maps_to_axis():
    parser = build_parser()
    # motors A/B name the drivers; they map to stepper_x/stepper_y internally
    assert parser.parse_args(_gcode_args(['collect', 'MOTOR=A', 'SPEED=55'], FLAGS)).axis == 'x'
    assert parser.parse_args(_gcode_args(['collect', 'MOTOR=B', 'SPEED=55'], FLAGS)).axis == 'y'
    assert parser.parse_args(_gcode_args(['tune', 'MOTOR=AB'], FLAGS)).axis == 'xy'
    # the old AXIS= keeps working
    assert parser.parse_args(_gcode_args(['collect', 'AXIS=Y', 'SPEED=55'], FLAGS)).axis == 'y'


def test_motor_label():
    from chopper_autotune.collect import motor_label
    assert motor_label('x') == 'A'
    assert motor_label('y') == 'B'


def test_sigterm_handler_raises_systemexit():
    install_sigterm_handler()
    handler = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)


def test_latest_dataset(tmp_path):
    base = tmp_path / 'datasets'
    stamp(Dataset.create(base / '20260701_120000_x', {}), 1000)
    stamp(Dataset.create(base / '20260703_090000_y', {}), 2000)
    (base / 'not_a_dataset').mkdir()

    assert latest_dataset(bases=(base,)) == str(base / '20260703_090000_y')


def stamp(ds, mtime):
    import os
    os.utime(ds.manifest_path, (mtime, mtime))
    return ds


def test_latest_dataset_skips_scans_and_demos(tmp_path):
    base = tmp_path / 'datasets'
    stamp(Dataset.create(base / '01_x', {'search': 'grid'}), 1000)
    stamp(Dataset.create(base / '02_speed_x', {'mode': 'find-speed'}), 2000)
    stamp(Dataset.create(base / '03_demo_x', {'mode': 'demo'}), 3000)

    # a fresher demo/scan must not become "the latest run" for analyze/save
    assert latest_dataset(bases=(base,)) == str(base / '01_x')


def test_dataset_dirs_order_by_data_mtime_not_name(tmp_path):
    from chopper_autotune.analyze import dataset_dirs
    base = tmp_path / 'datasets'
    custom = stamp(Dataset.create(base / 'mytest', {}), 1000)      # older, name sorts last
    newer = stamp(Dataset.create(base / '20260704_120000_x', {}), 2000)

    assert dataset_dirs(bases=(base,)) == [custom.root, newer.root]


def test_latest_dataset_missing(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        latest_dataset(bases=(tmp_path / 'nope',))
