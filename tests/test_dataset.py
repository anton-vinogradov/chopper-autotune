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


def test_records_skip_corrupt_tail_line(tmp_path, capsys):
    ds = Dataset.create(tmp_path / 'ds', {})
    ds.append({'id': 'a', 'status': 'ok'})
    with ds.records_path.open('a') as f:
        f.write('{"id": "b", "status"')          # power loss mid-append
    assert [r['id'] for r in ds.records()] == ['a']
    assert ds.done_ids() == {'a'}
    assert 'corrupt line' in capsys.readouterr().out


def test_update_manifest_merges(tmp_path):
    ds = Dataset.create(tmp_path / 'ds', {'driver': '2209'})
    ds.update_manifest(winner={'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14})
    assert Dataset.open(ds.root).manifest() == {
        'driver': '2209', 'winner': {'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}}


def test_raw_storage_roundtrip(tmp_path):
    ds = Dataset.create(tmp_path / 'ds', {})

    rel = ds.store_raw_samples('m1', [[0.1, 1, 2, 3], [0.2, 4, 5, 6]])
    record = {'id': 'm1', 'raw': rel}
    with ds.open_raw(record) as f:
        lines = f.read().splitlines()
    assert lines[0] == '#time,accel_x,accel_y,accel_z'
    assert lines[1] == '0.100000,1.000000,2.000000,3.000000'
    assert len(lines) == 3
