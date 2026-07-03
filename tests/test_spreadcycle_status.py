import argparse
from datetime import datetime, timedelta, timezone

import pytest

from chopper_autotune import tmc
from chopper_autotune.analyze import newest_dataset, run_status
from chopper_autotune.collect import detect_hardware
from chopper_autotune.dataset import Dataset


class FakeKlippy:
    def __init__(self, settings):
        self._settings = settings

    def settings(self):
        return self._settings


def make_settings(driver='2209', extra_tmc=None):
    tmc_section = {'driver_tbl': 2, 'driver_toff': 3, 'driver_hstrt': 5, 'driver_hend': 0}
    tmc_section.update(extra_tmc or {})
    return {
        'printer': {'kinematics': 'corexy', 'max_accel': 10000},
        'stepper_x': {'position_min': 0, 'position_max': 260},
        'stepper_y': {'position_min': 0, 'position_max': 260},
        'tmc%s stepper_x' % driver: tmc_section,
        'resonance_tester': {'accel_chip': 'adxl345'},
    }


def test_stealthchop_detected_and_switch_selected():
    hw = detect_hardware(FakeKlippy(make_settings(extra_tmc={'stealthchop_threshold': 999})), 'x')
    assert hw.stealth == ('en_spreadcycle', 1, 0)


def test_spreadcycle_printer_needs_no_forcing():
    assert detect_hardware(FakeKlippy(make_settings()), 'x').stealth is None
    zero = make_settings(extra_tmc={'stealthchop_threshold': 0})
    assert detect_hardware(FakeKlippy(zero), 'x').stealth is None


def test_switch_polarity_per_driver():
    assert tmc.DRIVERS['5160'].spreadcycle_switch == ('en_pwm_mode', 0, 1)
    assert tmc.DRIVERS['2209'].spreadcycle_switch == ('en_spreadcycle', 1, 0)
    assert tmc.DRIVERS['2660'].spreadcycle_switch is None


def make_running_dataset(tmp_path, n=20):
    ds = Dataset.create(tmp_path / 'ds', {
        'driver': '2209', 'stepper': 'stepper_x', 'search': 'grid', 'capture': 'stream',
        'iterations': 1, 'speeds': [58],
        'ranges': {'tbl': [0, 0], 'toff': [3, 4], 'hstrt': [4, 4], 'hend': [0, 15],
                   'tpfd': None},
    })
    start = datetime.now(timezone.utc) - timedelta(seconds=2 * n)
    for i in range(n):
        ds.append({'id': 'm%d' % i, 'kind': 'move', 'status': 'ok', 'tbl': 0, 'toff': 3,
                   'hstrt': 4, 'hend': i % 16, 'score': {'median_magnitude': 1000.0},
                   'ts': (start + timedelta(seconds=2 * i)).isoformat(timespec='seconds')})
    return ds


def test_status_reports_pace_and_eta(tmp_path, capsys):
    ds = make_running_dataset(tmp_path)
    args = argparse.Namespace(dataset=str(ds.root), total=None)
    assert run_status(args) == 0
    out = capsys.readouterr().out
    assert 'Measurements: 20 ok, 0 failed' in out
    assert 'Pace: 2.0 s/move' in out
    # 2 toff x 13 hend (hstrt=4 limits hend to 0..12) = 26 combos -> 52 planned moves
    assert 'Progress: 20/52 (38%)' in out


def test_newest_dataset_picks_latest(tmp_path):
    old = Dataset.create(tmp_path / 'a', {})
    old.append({'id': 'x', 'status': 'ok'})
    new = Dataset.create(tmp_path / 'b', {})
    new.append({'id': 'y', 'status': 'ok'})
    assert newest_dataset(bases=(tmp_path,)) == new.root
