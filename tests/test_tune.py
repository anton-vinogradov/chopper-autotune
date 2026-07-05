import argparse

import pytest

from chopper_autotune import tmc, tune
from chopper_autotune.collect import Range
from chopper_autotune.dataset import Dataset


def tune_args(**overrides):
    base = {'axis': 'xy', 'speed': None, 'save': False, 'iterations': 1,
            'audible_weight': 0.25, 'accel': None, 'no_raw': False, 'csv': False,
            'socket': None, 'url': 'http://x', 'dry_run': False}
    base.update(overrides)
    return argparse.Namespace(**base)


def make_dataset(tmp_path, axis, toff):
    ds = Dataset.create(tmp_path / axis, {
        'driver': '2209', 'stepper': 'stepper_' + axis, 'trim': 0.1})
    combo = tmc.Chopper(0, toff, 7, 5)
    ds.append({'id': 'a', 'kind': 'move', 'status': 'ok', **combo.fields(),
               'score': {'median_magnitude': 1000.0}})
    return str(ds.root)


def test_tune_runs_both_axes_and_seeds_second(tmp_path, monkeypatch, capsys):
    calls = {'scan': [], 'collect': []}
    roots = {'x': make_dataset(tmp_path, 'x', 8), 'y': make_dataset(tmp_path, 'y', 6)}

    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)

    def fake_scan(kl, args):
        calls['scan'].append(args.axis)
        return 0, {'x': 58, 'y': 52}[args.axis]

    def fake_collect(kl, args):
        calls['collect'].append((args.axis, args.speed, args.seed_from))
        return 0, roots[args.axis]

    monkeypatch.setattr(tune, 'scan', fake_scan)
    monkeypatch.setattr(tune, 'collect', fake_collect)

    assert tune.run_tune(tune_args()) == 0
    assert calls['scan'] == ['x', 'y']
    assert calls['collect'] == [('x', Range(58, 58), None),
                                ('y', Range(52, 52), roots['x'])]
    out = capsys.readouterr().out
    assert '[tmc2209 stepper_x]' in out and 'driver_TOFF: 8' in out
    assert '[tmc2209 stepper_y]' in out and 'driver_TOFF: 6' in out
    assert 'SAVE=1' in out


def test_tune_single_axis_with_explicit_speed(tmp_path, monkeypatch):
    roots = {'y': make_dataset(tmp_path, 'y', 5)}
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args: pytest.fail('scan must be skipped'))
    monkeypatch.setattr(tune, 'collect', lambda kl, args: (0, roots[args.axis]))

    assert tune.run_tune(tune_args(axis='y', speed=Range(52, 52))) == 0


def test_tune_save_batches_all_winners(tmp_path, monkeypatch):
    roots = {'x': make_dataset(tmp_path, 'x', 8), 'y': make_dataset(tmp_path, 'y', 6)}
    saved = []
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args: (0, 58))
    monkeypatch.setattr(tune, 'collect', lambda kl, args: (0, roots[args.axis]))
    monkeypatch.setattr(tune, 'Moonraker', lambda url: '<mk>')
    monkeypatch.setattr('chopper_autotune.analyze.run_save',
                        lambda mk, items: saved.extend(items))

    tune.run_tune(tune_args(save=True))
    assert [manifest['stepper'] for manifest, _ in saved] == ['stepper_x', 'stepper_y']
    assert saved[0][1] == tmc.Chopper(0, 8, 7, 5)


def test_winner_of_prefers_the_recorded_winner(tmp_path):
    # the run's validated recommendation, not a full re-rank that can surface
    # an unvalidated lucky combo (winner's curse)
    root = make_dataset(tmp_path, 'x', 8)
    Dataset.open(root).update_manifest(winner={'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14})
    _, combo = tune.winner_of(root, 0.25)
    assert combo == tmc.Chopper(2, 1, 4, 14)


def test_winner_of_falls_back_to_ranking(tmp_path):
    root = make_dataset(tmp_path, 'x', 8)          # pre-winner dataset
    _, combo = tune.winner_of(root, 0.25)
    assert combo == tmc.Chopper(0, 8, 7, 5)


def test_tune_aborts_without_resonance_peak(monkeypatch):
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args: (0, None))

    with pytest.raises(SystemExit, match='no clear resonance peak'):
        tune.run_tune(tune_args())
