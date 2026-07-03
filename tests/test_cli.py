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


def test_sigterm_handler_raises_systemexit():
    install_sigterm_handler()
    handler = signal.getsignal(signal.SIGTERM)
    with pytest.raises(SystemExit):
        handler(signal.SIGTERM, None)


def test_latest_dataset(tmp_path):
    base = tmp_path / 'datasets'
    Dataset.create(base / '20260701_120000_x', {})
    Dataset.create(base / '20260703_090000_y', {})
    (base / 'not_a_dataset').mkdir()

    assert latest_dataset(bases=(base,)) == str(base / '20260703_090000_y')


def test_latest_dataset_missing(tmp_path):
    import pytest
    with pytest.raises(SystemExit):
        latest_dataset(bases=(tmp_path / 'nope',))
