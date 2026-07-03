import argparse

import pytest

import chopper_autotune.demo as demo_module
from chopper_autotune import tmc
from chopper_autotune.cli import _chopper
from chopper_autotune.collect import Hardware, Range
from chopper_autotune.demo import bar, demo


def test_bar_scales_and_has_min_width():
    assert bar(100, 100) == '#' * 40
    assert bar(50, 100) == '#' * 20
    assert bar(1, 100) == '#'          # never empty


def test_chopper_parser():
    assert _chopper('2,3,5,0') == tmc.Chopper(2, 3, 5, 0)
    assert _chopper('0/2/7/9') == tmc.Chopper(0, 2, 7, 9)
    with pytest.raises(argparse.ArgumentTypeError):
        _chopper('2,3,5')


def make_hw(baseline):
    return Hardware(kl=None, stepper='stepper_x', driver=tmc.DRIVERS['2209'],
                    accel_chip='adxl345', kinematics='corexy', axis_span=260,
                    center=(130, 130), max_accel=10000, baseline=baseline)


def demo_args(**over):
    base = dict(axis='x', speed=None, default=None, iterations=3, measure_time=1.0,
                accel=None, trim=None, socket=None, dry_run=True)
    base.update(over)
    return argparse.Namespace(**base)


def test_demo_rejects_when_not_yet_tuned(monkeypatch):
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 0}))
    with pytest.raises(SystemExit, match='nothing to demo'):
        demo(None, demo_args())


def test_demo_dry_run_reports_plan(monkeypatch, capsys):
    monkeypatch.setattr(demo_module, 'detect_hardware',
                        lambda kl, axis: make_hw({'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14}))
    assert demo(None, demo_args(speed=Range(58, 58))) == 0
    out = capsys.readouterr().out
    assert 'defaults tbl2_toff3_hstrt5_hend0 vs tuned tbl2_toff1_hstrt4_hend14' in out
