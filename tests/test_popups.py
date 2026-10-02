"""A run pops up once, at its end: an M118 (`echo:`, a KlipperScreen popup) while the
motors still have work covers the panel mid-run. The real tune, scan and collect run
here; the hardware and the measurements are stubbed."""
import pytest

import chopper_autotune.collect as col
import chopper_autotune.demo as demo
import chopper_autotune.extruder as ext
import chopper_autotune.find_speed as fs
from chopper_autotune import tmc, tune
from chopper_autotune.cli import build_parser


class FakeKl:
    path = '<sock>'

    def __init__(self, log):
        self.log = log

    def connect(self):
        return self

    def close(self):
        pass

    def settings(self):
        return {}

    def gcode(self, script):
        self.log.append(script)

    def gcode_output(self, script):
        return []

    def subscribe_accel(self, chip):
        pass

    def info(self):
        return {'software_version': 'v0.13.0'}

    def request(self, method, params=None):
        return {'status': {'gcode': {'commands': {'M117': {}, 'M118': {}, 'RESPOND': {}}}}}


class Guard:
    def __init__(self, *args):
        pass

    def preflight(self):
        pass


@pytest.fixture
def printer(tmp_path, monkeypatch):
    log = []

    def hardware(kl, axis):
        return col.Hardware(kl=kl, stepper='stepper_' + axis, driver=tmc.DRIVERS['2209'],
                            accel_chip='adxl345', kinematics='corexy', axis_span=260,
                            center=(130, 130), max_accel=10000,
                            baseline={'tbl': 0, 'toff': 2, 'hstrt': 2, 'hend': 12}, display=True)

    stamp = iter(range(100))
    for module in (col, fs):
        for name, value in (('detect_hardware', hardware), ('ThermalGuard', Guard),
                            ('refuse_blind_z_hop', lambda *args: None),
                            ('park', lambda *args: None),
                            ('make_parker', lambda *args: (lambda *more: None)),
                            ('measure_baseline', lambda *args: None),
                            ('enter_spreadcycle', lambda *args: None),
                            ('exit_spreadcycle', lambda *args: None),
                            ('restore_chopper', lambda *args: None),
                            ('rehome_unless_hot', lambda *args: None),
                            ('refuse_if_printing', lambda *args: None),
                            ('default_dataset_root',
                             lambda stem: tmp_path / ('%02d_%s' % (next(stamp), stem)))):
            monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(demo, 'write_state', lambda *args: None)
    monkeypatch.setattr(ext, 'load_winner_state', lambda: None)

    def scan_move(hw, ds, args, record, speed, cruise, travel, direction, accel, before_move):
        log.append('MOVE')
        record.update(status='ok', score={'median_magnitude': 2000.0 - 30 * abs(speed - 60)})
        ds.append(record)
        return record

    def register_move(hw, ds, args, combo, speed, iteration, direction, travel, accel, before_move):
        log.append('MOVE')
        record = {'id': col.measurement_id(combo, speed, iteration, direction), 'kind': 'move',
                  **combo.fields(), 'speed': speed, 'direction': direction,
                  'iteration': iteration, 'status': 'ok',
                  'score': {'median_magnitude': 100.0 + abs(combo.toff - 5) * 10
                            + abs(combo.hend - 6) * 7 + combo.tbl * 3}}
        ds.append(record)
        return record

    monkeypatch.setattr(fs, 'measure_move', scan_move)
    monkeypatch.setattr(col, 'run_measurement', register_move)
    monkeypatch.setattr(tune, 'Klippy', lambda sock: FakeKl(log))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    return log


def popups_after_the_last_move(log: 'list[str]') -> 'tuple[int, int]':
    last_move = max(index for index, line in enumerate(log) if line == 'MOVE')
    popups = [index for index, line in enumerate(log) if line.startswith('M118')]
    return len(popups), sum(index > last_move for index in popups)


def test_a_tune_pops_up_once_at_its_end(printer):
    tune.run_tune(build_parser().parse_args(['tune', '--motor', 'ab', '--no-raw']))
    assert popups_after_the_last_move(printer) == (1, 1)


def test_a_grid_collect_pops_up_once_after_its_validation(printer, tmp_path):
    args = build_parser().parse_args(['collect', '--axis', 'x', '--speed', '60', '--tbl', '0:0',
                                      '--toff', '3:5', '--hstrt', '3:3', '--hend', '5:6',
                                      '--yes', '--no-raw', '--dataset', str(tmp_path / 'grid')])
    col.collect(FakeKl(printer), args)
    assert popups_after_the_last_move(printer) == (1, 1)
