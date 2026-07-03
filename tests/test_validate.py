import argparse

from chopper_autotune import tmc
from chopper_autotune.collect import (Hardware, Screen, VALIDATE_EXTRA_ITERATIONS,
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

    def fake_measurement(hw, ds_, args, combo, speed, iteration, direction, travel, accel,
                         before_move):
        calls.append((combo.toff, iteration, direction))
        record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
                  'status': 'ok', **combo.fields(),
                  'score': {'median_magnitude': 800.0 + combo.toff}}
        ds_.append(record)
        return record

    monkeypatch.setattr(collect_module, 'run_measurement', fake_measurement)
    ok, failed = validate_top(kl, make_hw(kl), ds, make_args(validate=2), [58], 70.0, 1000,
                              set(), lambda: None, Screen(kl, display=False))

    # 2 finalists (toff 5 and 6) x 2 extra iterations x 2 directions; they stay on
    # top after validation, so the round loop converges without touching anyone else
    assert ok == 8 and failed == 0
    assert {c[0] for c in calls} == {5, 6}
    assert {c[1] for c in calls} == {1, 2}
    out = capsys.readouterr().out
    assert 'Validating 2 candidate' in out
    assert 'Recommended for printer.cfg' in out


def test_validate_top_skips_done_ids(tmp_path, monkeypatch):
    ds = seed_dataset(tmp_path)
    kl = FakeKl()
    calls = []

    def fake_measurement(hw, ds_, args, combo, speed, iteration, direction, travel, accel,
                         before_move):
        calls.append(combo.toff)
        record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
                  'status': 'ok', **combo.fields(), 'score': {'median_magnitude': 900.0}}
        ds_.append(record)
        return record

    monkeypatch.setattr(collect_module, 'run_measurement', fake_measurement)
    best = tmc.Chopper(0, 5, 4, 4)
    done = {measurement_id(best, 58, i, d) for i in (1, 2) for d in (1, -1)}
    ok, _ = validate_top(kl, make_hw(kl), ds, make_args(validate=1), [58], 70.0, 1000,
                         done, lambda: None, Screen(kl, display=False))
    assert ok == 0 and calls == []


def test_validate_extra_iterations_constant():
    assert VALIDATE_EXTRA_ITERATIONS == 2


def validation_fake(true_of):
    """Fake measurement: iteration 0 is the seeded value; validation iterations
    (>=1) return true_of[combo.toff], so lucky seeds regress on re-measurement."""
    def fake(hw, ds_, args, combo, speed, iteration, direction, travel, accel, before_move):
        record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
                  'status': 'ok', **combo.fields(),
                  'score': {'median_magnitude': float(true_of[combo.toff])}}
        ds_.append(record)
        return record
    return fake


def seed_grid(tmp_path, seed_of):
    ds = Dataset.create(tmp_path / 'ds', {})
    for toff, magnitude in seed_of.items():
        combo = tmc.Chopper(0, toff, 4, 4)
        for direction in ('fwd', 'rev'):
            ds.append({'id': '%s_v58_i0_%s' % (combo.label(), direction), 'kind': 'move',
                       'status': 'ok', **combo.fields(),
                       'score': {'median_magnitude': float(magnitude)}})
    return ds


def test_validate_recommends_consistent_combo_not_lucky_seed(tmp_path, monkeypatch, capsys):
    # toff4 is measured consistently; the others got lucky-low n=2 seeds and regress
    # hard on validation. The recommendation must be toff4, not a lucky seed.
    seed = {2: 1000, 3: 1010, 4: 1020, 5: 1030, 6: 1040, 7: 1050}
    true = {2: 1600, 3: 1600, 4: 1022, 5: 1600, 6: 1045, 7: 1600}
    ds = seed_grid(tmp_path, seed)
    kl = FakeKl()
    monkeypatch.setattr(collect_module, 'run_measurement', validation_fake(true))

    validate_top(kl, make_hw(kl), ds, make_args(validate=2), [58], 70.0, 1000,
                 set(), lambda d, t: None, Screen(kl, display=False))
    snippet = capsys.readouterr().out.split('Recommended for printer.cfg:')[1]
    assert 'driver_TOFF: 4' in snippet


def test_validate_recommends_only_from_validated_set(tmp_path, monkeypatch):
    # every validation returns worse than every seed, so after the round budget the
    # whole-grid rank-1 is an unvalidated lucky seed; the recommendation must still
    # be a combo we actually re-measured (n > 2), never that untouched lucky seed.
    seed = {t: 1000 + t for t in range(2, 16)}
    true = {t: 3000 for t in range(2, 16)}
    ds = seed_grid(tmp_path, seed)
    kl = FakeKl()
    monkeypatch.setattr(collect_module, 'run_measurement', validation_fake(true))

    validate_top(kl, make_hw(kl), ds, make_args(validate=2), [58], 70.0, 1000,
                 set(), lambda d, t: None, Screen(kl, display=False))

    from chopper_autotune.analyze import aggregate, rank
    ranked = rank(aggregate(ds, False, 0.1), tmc.DRIVERS['2209'], 0.25)
    validated = {a['chopper'] for a in ranked if a['n'] > 2}
    # the untouched lucky seeds still top the whole-grid ranking
    assert ranked[0]['chopper'] not in validated
    # 4 rounds x 2 per round = 8 combos validated, none left at magnitude 3000-free luck
    assert len(validated) == 8


def test_parker_parks_before_drift_reaches_rail():
    parks = []
    kl = FakeKl()
    hw = make_hw(kl)
    import chopper_autotune.collect as cm
    original = cm.park
    cm.park = lambda kl_, hw_: parks.append(True)
    try:
        before_move = cm.make_parker(kl, hw)
        # span 260 -> headroom 120; alternating moves never park
        for _ in range(3):
            before_move(1, 104.0)
            before_move(-1, 104.0)
        assert parks == []
        # a same-direction retry would put net at 208 > 120: must re-park first
        before_move(1, 104.0)
        before_move(1, 104.0)
        assert parks == [True]
    finally:
        cm.park = original
