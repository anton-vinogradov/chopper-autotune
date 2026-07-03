from chopper_autotune.analyze import latest_dataset
from chopper_autotune.cli import _gcode_args
from chopper_autotune.dataset import Dataset


def test_gcode_args_translation():
    assert _gcode_args(['collect', 'AXIS=X', 'SPEED=55', 'MEASURE_TIME=1.5']) \
        == ['collect', '--axis', 'X', '--speed', '55', '--measure-time', '1.5']
    assert _gcode_args(['collect', 'TOFF=1:8']) == ['collect', '--toff', '1:8']


def test_gcode_args_flags():
    assert _gcode_args(['collect', 'DRY_RUN=1']) == ['collect', '--dry-run']
    assert _gcode_args(['collect', 'DRY_RUN=0']) == ['collect']
    assert _gcode_args(['analyze', 'APPLY=true', 'TOP=20']) == ['analyze', '--apply', '--top', '20']


def test_gcode_args_passthrough():
    args = ['analyze', 'datasets/20260703_x', '--top', '5']
    assert _gcode_args(args) == args


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
