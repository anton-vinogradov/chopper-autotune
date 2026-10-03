import os
from types import SimpleNamespace

import pytest

from chopper_autotune import (analyze, belts, collect, current, dataset, demo, envelope, extruder,
                              find_speed, resonance_map, tmc, tune)


@pytest.fixture(autouse=True)
def current_klipper(request, monkeypatch):
    """The fake printers run a supported Klipper; tests marked version_gate check the
    gate itself (collect.require_current_klipper)."""
    if 'version_gate' not in request.keywords:
        monkeypatch.setattr(collect, 'require_current_klipper', lambda kl: None)


@pytest.fixture(autouse=True)
def results_home(tmp_path, monkeypatch):
    """No test writes the user's ~/printer_data: the results, states and datasets of the
    tools go to the test's temp dir (a whole CHOPPER_CURRENT run left its current.json)."""
    home = tmp_path / 'chopper-autotune'
    for module in (dataset, collect, analyze):
        monkeypatch.setattr(module, 'RESULTS_HOME', home)
    for module, name in ((belts, 'STATE'), (belts, 'SWEEP_STATE'), (current, 'STATE'),
                         (envelope, 'STATE'), (extruder, 'STATE'), (resonance_map, 'STATE')):
        monkeypatch.setattr(module, name, str(home / os.path.basename(getattr(module, name))))


@pytest.fixture(autouse=True)
def lcd_row(monkeypatch):
    """Every display text the tools hand the Screen in any test fits a 16-character LCD
    row, the display on or off: a run with the display off hid one that did not."""
    update, final = collect.Screen.update, collect.Screen.final

    def fits(text):
        shown = collect.display_text(text)
        assert len(shown) <= collect.LCD_WIDTH, shown

    def checked_update(screen, text, force=False, short=None):
        fits(short or text)
        return update(screen, text, force, short)

    def checked_final(screen, text, short=None):
        fits(short or text)
        return final(screen, text, short)
    monkeypatch.setattr(collect.Screen, 'update', checked_update)
    monkeypatch.setattr(collect.Screen, 'final', checked_final)


def pytest_configure(config):
    config.addinivalue_line('markers', 'version_gate: runs the real Klipper version check')


class StubKl:
    path = '<sock>'

    def __init__(self, printer):
        self.printer = printer

    def connect(self):
        return self

    def close(self):
        pass

    def settings(self):
        return self.printer.settings

    def gcode(self, script):
        self.printer.log.append(script)

    def gcode_output(self, script):
        return []

    def subscribe_accel(self, chip):
        pass

    def info(self):
        return {'software_version': 'v0.13.0'}

    def request(self, method, params=None):
        return {'status': {'gcode': {'commands': {'M117': {}, 'M118': {}, 'RESPOND': {}}}}}


class StubGuard:
    def __init__(self, *args):
        pass

    def preflight(self):
        pass


@pytest.fixture
def stub_printer(tmp_path, monkeypatch):
    """A CoreXY with TMC2209s for the real tune, scan and collect: the hardware and the
    measurements are stubbed. `log` keeps the g-code sent and a 'MOVE' per measured
    move, `measured` the registers each register move ran, `roots` the datasets in the
    order the runs made them; the quietest chopper is toff 5, hend 6, tbl 0."""
    printer = SimpleNamespace(log=[], measured=[], roots=[], settings={})
    printer.kl = lambda: StubKl(printer)

    def dataset_root(stem):
        printer.roots.append(tmp_path / ('%02d_%s' % (len(printer.roots), stem)))
        return printer.roots[-1]

    def hardware(kl, axis):
        return collect.Hardware(kl=kl, stepper='stepper_' + axis, driver=tmc.DRIVERS['2209'],
                                accel_chip='adxl345', kinematics='corexy', axis_span=260,
                                center=(130, 130), max_accel=10000,
                                baseline={'tbl': 0, 'toff': 2, 'hstrt': 2, 'hend': 12},
                                display=True)

    for module in (collect, find_speed, resonance_map):
        for name, value in (('detect_hardware', hardware), ('ThermalGuard', StubGuard),
                            ('refuse_blind_z_hop', lambda *args: None),
                            ('park', lambda *args: None),
                            ('make_parker', lambda *args: (lambda *more: None)),
                            ('measure_baseline', lambda *args: None),
                            ('enter_spreadcycle', lambda *args, **kwargs: None),
                            ('exit_spreadcycle', lambda *args: None),
                            ('restore_chopper', lambda *args: None),
                            ('rehome_unless_hot', lambda *args: None),
                            ('refuse_if_printing', lambda *args: None),
                            ('default_dataset_root', dataset_root)):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
    monkeypatch.setattr(demo, 'write_state', lambda *args: None)
    monkeypatch.setattr(extruder, 'load_winner_state', lambda: None)

    def scan_move(hw, ds, args, record, speed, cruise, travel, direction, accel, before_move):
        printer.log.append('MOVE')
        record.update(status='ok', score={'median_magnitude': 2000.0 - 30 * abs(speed - 60)})
        ds.append(record)
        return record

    def register_move(hw, ds, args, combo, speed, iteration, direction, travel, accel, before_move):
        printer.log.append('MOVE')
        printer.measured.append(combo)
        record = {'id': collect.measurement_id(combo, speed, iteration, direction), 'kind': 'move',
                  **combo.fields(), 'speed': speed, 'direction': direction,
                  'iteration': iteration, 'status': 'ok',
                  'score': {'median_magnitude': 100.0 + abs(combo.toff - 5) * 10
                            + abs(combo.hend - 6) * 7 + combo.tbl * 3}}
        ds.append(record)
        return record

    monkeypatch.setattr(find_speed, 'measure_move', scan_move)
    monkeypatch.setattr(collect, 'run_measurement', register_move)
    monkeypatch.setattr(tune, 'Klippy', lambda sock: StubKl(printer))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    return printer
