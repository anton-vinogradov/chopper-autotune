import pytest

from chopper_autotune.dataset import Dataset


def test_roundtrip_and_resume(tmp_path):
    root = tmp_path / 'ds'
    ds = Dataset.create(root, {'driver': '2209'})
    ds.append({'id': 'a', 'status': 'ok'})
    ds.append({'id': 'b', 'status': 'failed'})

    reopened = Dataset.open(root)
    assert reopened.manifest() == {'driver': '2209'}
    assert len(reopened.records()) == 2
    assert reopened.done_ids() == {'a'}

    Dataset.create(root, {'driver': 'other'})
    assert Dataset.open(root).manifest() == {'driver': '2209'}


def test_open_missing_dataset(tmp_path):
    with pytest.raises(SystemExit):
        Dataset.open(tmp_path / 'nope')


def test_raw_storage_roundtrip(tmp_path):
    ds = Dataset.create(tmp_path / 'ds', {})

    rel = ds.store_raw_samples('m1', [[0.1, 1, 2, 3], [0.2, 4, 5, 6]])
    record = {'id': 'm1', 'raw': rel}
    with ds.open_raw(record) as f:
        lines = f.read().splitlines()
    assert lines[0] == '#time,accel_x,accel_y,accel_z'
    assert lines[1] == '0.100000,1.000000,2.000000,3.000000'
    assert len(lines) == 3
