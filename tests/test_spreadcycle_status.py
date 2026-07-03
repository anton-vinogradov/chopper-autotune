import argparse
from datetime import datetime, timedelta, timezone

from chopper_autotune import tmc
from chopper_autotune.analyze import newest_dataset, run_status
from chopper_autotune.collect import Screen, detect_hardware
from chopper_autotune.dataset import Dataset
from chopper_autotune.klippy import KlippyError


class FakeKlippy:
    def __init__(self, settings=None, fail=False):
        self._settings = settings
        self.fail = fail
        self.scripts = []

    def settings(self):
        return self._settings

    def gcode(self, script):
        if self.fail:
            raise KlippyError('no display')
        self.scripts.append(script)


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


def test_display_detected_from_config():
    settings = make_settings()
    assert detect_hardware(FakeKlippy(settings), 'x').display is False
    settings['display_status'] = {}
    assert detect_hardware(FakeKlippy(settings), 'x').display is True


def test_screen_throttles_and_forces():
    kl = FakeKlippy()
    screen = Screen(kl, enabled=True)
    screen.update('one')
    screen.update('two')
    screen.update('three', force=True)
    assert kl.scripts == ['M117 one', 'M117 three']


def test_screen_disabled_and_error_paths():
    kl = FakeKlippy()
    Screen(kl, enabled=False).update('nope', force=True)
    assert kl.scripts == []

    broken = Screen(FakeKlippy(fail=True), enabled=True)
    broken.update('boom', force=True)
    assert broken.enabled is False


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
    # 2 toff x 15 hend (hstrt=4 limits raw hend to 0..14) = 30 combos -> 60 planned moves
    assert 'Progress: 20/60 (33%)' in out


def test_status_pace_ignores_old_pause(tmp_path, capsys):
    ds = Dataset.create(tmp_path / 'ds', {'driver': '2209', 'stepper': 'stepper_x'})
    start = datetime.now(timezone.utc) - timedelta(hours=3)
    for i in range(60):
        # one-hour pause in the middle must not skew the pace of the recent window
        offset = timedelta(seconds=2 * i) + (timedelta(hours=1) if i >= 10 else timedelta())
        ds.append({'id': 'm%d' % i, 'kind': 'move', 'status': 'ok', 'tbl': 0, 'toff': 3,
                   'hstrt': 4, 'hend': 0, 'score': {'median_magnitude': 1000.0},
                   'ts': (start + offset).isoformat(timespec='seconds')})

    run_status(argparse.Namespace(dataset=str(ds.root), total=None))
    assert 'Pace: 2.0 s/move (last 50 moves)' in capsys.readouterr().out


def test_newest_dataset_picks_latest(tmp_path):
    old = Dataset.create(tmp_path / 'a', {})
    old.append({'id': 'x', 'status': 'ok'})
    new = Dataset.create(tmp_path / 'b', {})
    new.append({'id': 'y', 'status': 'ok'})
    assert newest_dataset(bases=(tmp_path,)) == new.root
