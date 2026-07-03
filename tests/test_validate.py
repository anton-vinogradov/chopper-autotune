import argparse

from chopper_autotune import tmc
from chopper_autotune.collect import (Hardware, Screen, VALIDATE_EXTRA_ITERATIONS, collect,
                                      measurement_id, validate_top)
from chopper_autotune.dataset import Dataset

import chopper_autotune.collect as collect_module


class FakeKl:
    def __init__(self):
        self.scripts = []

    def gcode(self, script):
        self.scripts.append(script)


def make_hw(kl):
    return Hardware(kl=kl, stepper='stepper_x', driver=tmc.DRIVERS['2209'],
                    accel_chip='adxl345', kinematics='corexy', axis_span=260,
                    center=(130, 130), max_accel=10000, baseline={})


def make_args(validate=2):
    return argparse.Namespace(validate=validate, iterations=1, trim=0.1,
                              audible_weight=0.25, source='stream')


def seed_dataset(tmp_path):
    ds = Dataset.create(tmp_path / 'ds', {})
    for toff, magnitude in ((5, 900.0), (6, 1000.0), (7, 1100.0), (8, 1200.0)):
        combo = tmc.Chopper(0, toff, 4, 4)
        for direction in ('fwd', 'rev'):
            ds.append({'id': '%s_v58_i0_%s' % (combo.label(), direction), 'kind': 'move',
                       'status': 'ok', **combo.fields(),
                       'score': {'median_magnitude': magnitude}})
    return ds


def test_validate_top_remeasures_finalists(tmp_path, monkeypatch, capsys):
    ds = seed_dataset(tmp_path)
    kl = FakeKl()
    calls = []

    def fake_measurement(hw, ds_, args, combo, speed, iteration, direction, travel, accel):
        calls.append((combo.toff, iteration, direction))
        record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
                  'status': 'ok', **combo.fields(),
                  'score': {'median_magnitude': 800.0 + combo.toff}}
        ds_.append(record)
        return record

    monkeypatch.setattr(collect_module, 'run_measurement', fake_measurement)
    ok, failed = validate_top(kl, make_hw(kl), ds, make_args(validate=2), [58], 70.0, 1000,
                              set(), lambda: None, Screen(kl, enabled=False))

    # 2 finalists (toff 5 and 6) x 2 extra iterations x 2 directions
    assert ok == 8 and failed == 0
    assert {c[0] for c in calls} == {5, 6}
    assert {c[1] for c in calls} == {1, 2}
    out = capsys.readouterr().out
    assert 'Validating top 2' in out
    assert 'Recommended for printer.cfg' in out


def test_validate_top_skips_done_ids(tmp_path, monkeypatch):
    ds = seed_dataset(tmp_path)
    kl = FakeKl()
    calls = []

    def fake_measurement(hw, ds_, args, combo, speed, iteration, direction, travel, accel):
        calls.append(combo.toff)
        record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
                  'status': 'ok', **combo.fields(), 'score': {'median_magnitude': 900.0}}
        ds_.append(record)
        return record

    monkeypatch.setattr(collect_module, 'run_measurement', fake_measurement)
    best = tmc.Chopper(0, 5, 4, 4)
    done = {measurement_id(best, 58, i, d) for i in (1, 2) for d in (1, -1)}
    ok, _ = validate_top(kl, make_hw(kl), ds, make_args(validate=1), [58], 70.0, 1000,
                         done, lambda: None, Screen(kl, enabled=False))
    assert ok == 0 and calls == []


def test_validate_extra_iterations_constant():
    assert VALIDATE_EXTRA_ITERATIONS == 2
