"""The noise floor is captured once per dataset, with the motors off. #133's find-speed log
read it twice, 300.3 then 343.3: the sweep captured it again after enabling the stepper."""
import re

import numpy as np
import pytest

import chopper_autotune.collect as collect_mod
import chopper_autotune.find_speed as fs
import chopper_autotune.resonance_map as map_mod
from chopper_autotune import tmc
from chopper_autotune.cli import build_parser
from chopper_autotune.collect import Hardware
from chopper_autotune.dataset import Dataset

ENABLE = re.compile(r'SET_STEPPER_ENABLE STEPPER="?stepper_x"? ENABLE=([01])')


class MotorKl:
    """Records G-code and follows whether stepper_x is energized, the way Klipper does."""
    path = '<sock>'

    def __init__(self):
        self.scripts = []
        self.motor_on = True        # holding since the last job: only park() switches it off

    def settings(self):
        return {}

    def stepper_states(self):
        return {'stepper_x': True, 'stepper_y': True, 'stepper_z': True}

    def gcode(self, script):
        self.scripts.append(script)
        for line in script.split('\n'):
            enable = ENABLE.match(line)
            if enable:
                self.motor_on = enable.group(1) == '1'
            elif line.startswith(('G28', 'FORCE_MOVE STEPPER=stepper_x')):
                self.motor_on = True
            elif line.startswith(('M18', 'M84')):
                self.motor_on = True        # holding since the last job: only park() switches it off

    def gcode_output(self, script):
        return []

    def subscribe_accel(self, chip):
        pass

    def is_printing(self):
        return False


@pytest.fixture
def printer(monkeypatch):
    """A fake printer whose accelerometer reads 300 at rest and 343 with the motor
    energized (#133's two readings); returns (kl, captures, moves), the motor state at
    every noise-floor capture and at every sweep move."""
    kl = MotorKl()
    hw = Hardware(kl=kl, stepper='stepper_x', driver=tmc.DRIVERS['2209'],
                  accel_chip='adxl345', kinematics='corexy', axis_span=260,
                  center=(130, 130), max_accel=10000,
                  baseline={'tbl': 0, 'toff': 2, 'hstrt': 2, 'hend': 12})
    captures, moves = [], []

    def capture(hw_, script, duration):
        captures.append(kl.motor_on)
        t = np.linspace(0.0, duration, 3200)
        x = np.where(np.arange(len(t)) % 2, 1.0, -1.0) * (343.0 if kl.motor_on else 300.0)
        return t[-1], np.column_stack([t, x, np.zeros_like(t), np.zeros_like(t)])

    def move(hw_, ds, args, record, speed, cruise, travel, direction, accel, before):
        moves.append(kl.motor_on)
        record['status'] = 'ok'
        record['score'] = {'median_magnitude': float(speed)}    # still rising at the range top
        ds.append(record)
        return record

    monkeypatch.setattr(collect_mod, 'capture_stream', capture)
    monkeypatch.setattr(fs, 'measure_move', move)
    monkeypatch.setattr(fs, 'detect_hardware', lambda kl_, axis: hw)
    monkeypatch.setattr(map_mod, 'detect_hardware', lambda kl_, axis: hw)
    return kl, captures, moves


def noise_floors(root) -> 'list[float]':
    return [r['score']['median_magnitude'] for r in Dataset.open(root).records()
            if r['kind'] == 'baseline']


@pytest.mark.parametrize('resume', [False, True])
def test_find_speed_captures_the_noise_floor_once_with_the_motors_off(tmp_path, printer, resume):
    kl, captures, moves = printer
    root = tmp_path / 'ds'
    if resume:
        Dataset.create(root, {}).append({'id': 'baseline', 'kind': 'baseline', 'status': 'ok',
                                         'score': {'median_magnitude': 300.0}})
    args = build_parser().parse_args(
        ['find-speed', '--axis', 'x', '--yes', '--min-speed', '20', '--max-speed', '24',
         '--dataset', str(root), '--no-raw'])
    fs.scan(kl, args)

    assert args.max_speed > 24                  # the rising curve ran the extension rounds too
    assert captures == ([] if resume else [False])
    assert moves and all(moves)                 # the model saw the sweep enable the motor
    assert noise_floors(root) == [300.0]


def test_resonance_map_captures_the_noise_floor_once_with_the_motors_off(tmp_path, printer,
                                                                         monkeypatch):
    kl, captures, moves = printer
    monkeypatch.setattr(map_mod, 'STATE', str(tmp_path / 'map.json'))
    root = tmp_path / 'ds'
    args = build_parser().parse_args(
        ['map', '--axis', 'x', '--yes', '--min-speed', '20', '--max-speed', '40',
         '--dataset', str(root), '--no-raw'])
    map_mod.resonance_map(kl, args)

    assert captures == [False]
    assert moves and all(moves)
    assert noise_floors(root) == [300.0]
