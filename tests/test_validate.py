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

    def settings(self):
        return {}              # no X/Y driver sections: the thermal guard stays idle


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
        loud = 1000.0 if combo == tmc.DRIVERS['2209'].default else 0.0
        record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
                  'status': 'ok', **combo.fields(),
                  'score': {'median_magnitude': 800.0 + combo.toff + loud}}
        ds_.append(record)
        return record

    monkeypatch.setattr(collect_module, 'run_measurement', fake_measurement)
    ok, failed = validate_top(kl, make_hw(kl), ds, make_args(validate=2), [58], 70.0, 1000,
                              set(), lambda: None, Screen(kl, display=True))

    # 2 finalists (toff 5 and 6) x 2 extra iterations x 2 directions; they stay on
    # top after validation, so the round loop converges without touching anyone else;
    # then the winner and Klipper defaults (toff 3) in turns, 4 rounds x 2 directions
    rechecks = [c for c in calls if c[1] in (1, 2)]
    assert ok == 8 + 16 and failed == 0
    assert {c[0] for c in rechecks} == {5, 6} and len(rechecks) == 8
    assert sorted(c[:2] for c in calls if c[1] > 2) == sorted(
        (toff, i) for toff in (5, 3) for i in range(3, 7) for _ in (1, -1))
    out = capsys.readouterr().out
    assert 'Validating 2 candidate' in out
    assert 'Recommended for printer.cfg' in out
    # the recommendation is recorded so save/tune persist THIS combo later
    assert ds.manifest()['winner'] == {'tbl': 0, 'toff': 5, 'hstrt': 4, 'hend': 4}


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
    stock = tmc.stock_chopper(tmc.DRIVERS['2209'], False)
    done = {measurement_id(best, 58, i, d) for i in (1, 2) for d in (1, -1)} \
        | {measurement_id(c, 58, i, d) for c in (best, stock) for i in range(3, 7) for d in (1, -1)}
    ok, _ = validate_top(kl, make_hw(kl), ds, make_args(validate=1), [58], 70.0, 1000,
                         done, lambda: None, Screen(kl, display=True))
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
                 set(), lambda d, t: None, Screen(kl, display=True))
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
                 set(), lambda d, t: None, Screen(kl, display=True))

    from chopper_autotune.analyze import aggregate, rank
    ranked = rank(aggregate(ds, False, 0.1), tmc.DRIVERS['2209'], tmc.Hearing())
    stock = tmc.stock_chopper(tmc.DRIVERS['2209'], False)        # measured back to back
    validated = {a['chopper'] for a in ranked if a['n'] > 2 and a['chopper'] != stock}
    # the untouched lucky seeds still top the whole-grid ranking
    assert ranked[0]['chopper'] not in validated
    # 4 rounds x 2 per round = 8 combos validated, none left at magnitude 3000-free luck
    assert len(validated) == 8


def test_descent_rechecks_until_its_recommendation_tops_the_table(tmp_path, monkeypatch, capsys):
    # #164: the first three combos the descent meets read low once and regress on the
    # re-check, as tbl0_toff3_hstrt3_hend15_tpfd15 did; checked once, one of them was
    # recommended from below the ten rows the table shows
    order = []

    def lucky_first(hw, ds_, args, combo, speed, iteration, direction, travel, accel,
                    before_move):
        if combo not in order:
            order.append(combo)
        index = order.index(combo)
        value = (900.0 if iteration == 0 else 1300.0) if index < 3 else 1000.0 + index
        record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
                  'status': 'ok', **combo.fields(), 'score': {'median_magnitude': value}}
        ds_.append(record)
        return record

    monkeypatch.setattr(collect_module, 'run_measurement', lucky_first)
    ds = Dataset.create(tmp_path / 'ds', {})
    kl = FakeKl()
    args = argparse.Namespace(tbl=tmc.Range(1, 2), toff=tmc.Range(2, 5), hstrt=tmc.Range(0, 3),
                              hend=tmc.Range(0, 6), validate=3, iterations=1, trim=0.1,
                              seed_from=None, audible_weight=0.25, source='stream')
    collect_module.run_descent(kl, make_hw(kl), ds, args, None, [58], 70.0, 1000, set(),
                               lambda d, t: None, Screen(kl, display=True))

    from chopper_autotune.analyze import aggregate, rank
    ranked = rank(aggregate(ds, False, 0.1), tmc.DRIVERS['2209'], tmc.Hearing())
    winner = tmc.Chopper(**ds.manifest()['winner'])
    assert winner == ranked[0]['chopper'] and ranked[0]['n'] >= 6
    assert winner not in order[:3]
    table = capsys.readouterr().out.split('rank  tbl')[-1].split('Recommended for printer.cfg')[0]
    assert table.split('\n')[1].split()[1:5] == [str(winner.tbl), str(winner.toff),
                                                 str(winner.hstrt), str(winner.hend)]


def test_the_table_adds_the_recommended_row_at_its_own_rank(capsys):
    from chopper_autotune.analyze import print_table, rank
    ranked = rank([{'chopper': tmc.Chopper(0, toff, 4, 4), 'magnitude': 1000.0 + toff,
                    'spread': 10.0, 'n': 2} for toff in range(2, 16)], tmc.DRIVERS['2209'],
                  tmc.Hearing())
    print_table(ranked, 10, ranked[12])
    rows = capsys.readouterr().out.strip().split('\n')[1:]
    assert [row.split()[0] for row in rows] == [str(n) for n in range(1, 11)] + ['13']
    assert rows[-1].split()[2] == str(ranked[12]['chopper'].toff)
    print_table(ranked, 10, ranked[3])
    assert len(capsys.readouterr().out.strip().split('\n')) == 11


def drifting(true_of, drift):
    """Fake measurement: each combo's true magnitude, louder by `drift` per move made so
    far, as a printer heating over a run."""
    moves = []

    def fake(hw, ds_, args, combo, speed, iteration, direction, travel, accel, before_move):
        moves.append(combo)
        value = true_of(combo) * (1 + drift * len(moves))
        record = {'id': measurement_id(combo, speed, iteration, direction), 'kind': 'move',
                  'status': 'ok', **combo.fields(), 'speed': speed, 'direction': direction,
                  'iteration': iteration, 'score': {'median_magnitude': value}}
        ds_.append(record)
        return record
    return fake


def back_to_back_run(tmp_path, monkeypatch, true_of, drift, baseline=None):
    seed = {toff: 1000 + 10 * toff for toff in range(2, 8)}
    seed[2] = 700                                     # the pick's lucky first draw
    ds = seed_grid(tmp_path, seed)
    kl = FakeKl()
    hw = make_hw(kl)
    hw.baseline = baseline or {}
    finals = []
    screen = Screen(kl, display=True)               # its texts must fit the LCD (conftest)
    shown = screen.final
    screen.final = lambda text, short=None: (finals.append(text), shown(text, short))
    monkeypatch.setattr(collect_module, 'run_measurement', drifting(true_of, drift))
    validate_top(kl, hw, ds, make_args(validate=2), [58], 70.0, 1000, set(), lambda d, t: None,
                 screen)
    return ds.manifest(), finals[0]


def test_a_tie_under_drift_claims_no_gain_and_keeps_the_registers(tmp_path, monkeypatch, capsys):
    # #164: the winner's early lucky moves and defaults measured late, on a printer that
    # got twice as loud over the run, printed 1.5x where back to back it was 1.12x
    manifest, final = back_to_back_run(tmp_path, monkeypatch, lambda combo: 1000.0, drift=0.01)
    out = capsys.readouterr().out
    assert manifest['improvement'] is None and 'less vibration' not in final
    # the motor's registers are Klipper defaults here: the run says to keep them
    assert manifest['kept'] and manifest['winner'] == tmc.DRIVERS['2209'].default.fields()
    assert 'Keep your registers tbl2_toff3_hstrt5_hend0' in out and final.startswith('Chopper: keep')


def test_a_real_gain_is_claimed_at_its_size(tmp_path, monkeypatch, capsys):
    stock = tmc.DRIVERS['2209'].default
    manifest, final = back_to_back_run(
        tmp_path, monkeypatch, lambda combo: 1500.0 if combo == stock else 1000.0, drift=0.001)
    assert not manifest['kept'] and manifest['winner'] != stock.fields()
    assert abs(manifest['improvement'] - 1.5) < 0.03
    assert '33% less vibration' in final
    assert 'back to back: 1.5x less vibration, quieter in each of 4 rounds' in capsys.readouterr().out


def test_the_own_registers_stay_unless_the_winner_beats_them_every_round(tmp_path, monkeypatch,
                                                                        capsys):
    stock, own = tmc.DRIVERS['2209'].default, tmc.Chopper(1, 4, 2, 3)
    manifest, final = back_to_back_run(
        tmp_path, monkeypatch, lambda combo: 1500.0 if combo == stock else 1000.0, drift=0.01,
        baseline=own.fields())
    out = capsys.readouterr().out
    assert manifest['kept'] and manifest['winner'] == own.fields()
    assert 'Keep your registers tbl1_toff4_hstrt2_hend3' in out
    # what the kept registers bought over Klipper defaults is still measured back to back
    assert 1.4 < manifest['improvement'] < 1.6 and final.startswith('Chopper: keep tbl1_toff4')


def test_a_winner_that_ties_with_defaults_replaces_louder_registers_with_no_gain_claimed(
        tmp_path, monkeypatch, capsys):
    stock, own = tmc.DRIVERS['2209'].default, tmc.Chopper(1, 4, 2, 3)
    manifest, final = back_to_back_run(
        tmp_path, monkeypatch, lambda combo: 1200.0 if combo == own else 1000.0, drift=0.01,
        baseline=own.fields())
    assert not manifest['kept'] and manifest['winner'] not in (own.fields(), stock.fields())
    assert manifest['improvement'] is None and 'less vibration' not in final
    assert 'not quieter in each of 4 rounds, so no gain this run can tell from noise' \
        in capsys.readouterr().out


def test_whining_own_registers_give_way_though_they_shake_a_little_less(tmp_path, monkeypatch,
                                                                        capsys):
    # the rounds score as the ranking does: 24 kHz own registers at 950 count as 1188
    own = tmc.Chopper(2, 6, 4, 4)
    manifest, final = back_to_back_run(
        tmp_path, monkeypatch, lambda combo: 950.0 if combo == own else
        1500.0 if combo == tmc.DRIVERS['2209'].default else 1000.0, drift=0.0,
        baseline=own.fields())
    assert not manifest['kept'] and manifest['winner'] != own.fields()
    assert 'Keep your registers' not in capsys.readouterr().out


def test_klipper_defaults_that_beat_the_own_registers_become_the_recommendation(
        tmp_path, monkeypatch, capsys):
    stock, own = tmc.DRIVERS['2209'].default, tmc.Chopper(1, 4, 2, 3)
    manifest, final = back_to_back_run(
        tmp_path, monkeypatch, lambda combo: 800.0 if combo == stock else 1100.0, drift=0.0,
        baseline=own.fields())
    out = capsys.readouterr().out
    assert manifest['winner'] == stock.fields() and not manifest['kept']
    assert 'Klipper defaults tbl2_toff3_hstrt5_hend0 were quieter than' in out
    assert manifest['improvement'] is None              # no gain over themselves


def test_the_rounds_are_measured_afresh_every_time(tmp_path, monkeypatch):
    # a resumed run must not pair this session's moves with an earlier session's
    ds = seed_grid(tmp_path, {2: 1000, 3: 1100})
    kl = FakeKl()
    hw = make_hw(kl)
    monkeypatch.setattr(collect_module, 'run_measurement', drifting(lambda combo: 1000.0, 0.0))
    combos = [tmc.Chopper(0, 2, 4, 4), tmc.DRIVERS['2209'].default]
    taken = []
    for _ in range(2):
        rounds, ok, failed = collect_module.back_to_back(hw, ds, make_args(), combos, [58], 70.0,
                                                         1000, set(), lambda d, t: None)
        assert ok == 16 and failed == 0 and len(rounds[combos[0]]) == 4
        taken.append({record['id'] for record in ds.records()})
    assert len(taken[1] - taken[0]) == 16                  # no id of the first rounds again


def test_a_run_with_no_gain_clears_the_panel_figure(tmp_path, monkeypatch):
    import json

    from chopper_autotune import dataset as dataset_mod
    from chopper_autotune.demo import write_state
    write_state('x', tmc.DRIVERS['2209'].default, 1.3)       # an older run's figure
    back_to_back_run(tmp_path, monkeypatch, lambda combo: 1000.0, drift=0.01)
    state = json.loads((dataset_mod.RESULTS_HOME / 'state.json').read_text())
    assert state['x']['quieter'] is None


def test_parker_parks_before_drift_reaches_rail():
    parks = []
    kl = FakeKl()
    hw = make_hw(kl)
    import chopper_autotune.collect as cm
    original = cm.park
    cm.park = lambda kl_, hw_, release=True: parks.append(True)
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
