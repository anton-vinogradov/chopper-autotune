"""The scripts a printer with one motor per axis gets from COLLECT, FIND_SPEED and DEMO,
recorded from main before the two-motor rail (#129) and kept as they were: tools that learn
to move a rail must not change a line for a single motor. The printers are AWD's own
(tests/test_klipper_front.py) without the twins: meijjaa's cartesian with its low limits,
a Voron's CoreXY; each built from every release's modules."""
import os
import re

import pytest

import klipper_front
from chopper_autotune import collect, demo
from chopper_autotune.find_speed import scan
from test_klipper_front import SOURCES, assert_clean, awd_cfg, require, run_tool

REFERENCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'single_motor_scripts')
TUNED = 'driver_TBL: 1\ndriver_TOFF: 4\ndriver_HSTRT: 2\ndriver_HEND: 3\n'
RUNS = {
    'collect': (collect.collect, ['collect', '--axis', 'x', '--speed', '60', '--tbl', '1:1',
                                  '--toff', '3:4', '--hstrt', '4:4', '--hend', '3:3',
                                  '--iterations', '1', '--yes', '--no-raw']),
    'find-speed': (scan, ['find-speed', '--axis', 'x', '--min-speed', '30', '--max-speed', '90',
                          '--step', '10', '--yes', '--no-raw']),
    'find-speed-b': (scan, ['find-speed', '--axis', 'y', '--min-speed', '30', '--max-speed', '90',
                            '--step', '10', '--yes', '--no-raw']),
    'demo': (demo.demo, ['demo', '--axis', 'x', '--speed', '60', '--report', '--iterations', '1']),
    'demo-live': (demo.demo, ['demo', '--axis', 'x', '--speed', '60', '--rounds', '1',
                              '--repeats', '1']),
}
PRINTERS = ('meijjaa', 'voron-2209')


class PrinterClock(klipper_front.Clock):
    """The tools' clock on the printer's time: the display's pace and an ETA come out the
    same on every run."""

    def __init__(self, front):
        self.front = front

    def monotonic(self):
        return self.front.printer.reactor.monotonic()

    time = monotonic


def sent(source: str, printer: str, run: str, tmp_path, monkeypatch) -> str:
    """The scripts of a run, a blank line after each; the console fence's token (the
    process id) as <token>."""
    monkeypatch.setattr(klipper_front, 'CLOCK', [0.])
    cfg = awd_cfg(printer, twins=False)
    for section in re.findall(r'^\[(tmc\d+ stepper_[xy])\]$', cfg, re.MULTILINE):
        cfg = cfg.replace('[%s]\n' % section, '[%s]\n%s' % (section, TUNED))
    front = klipper_front.Front(source, cfg)
    monkeypatch.setattr(collect, 'time', PrinterClock(front))
    tool, argv = RUNS[run]
    if tool is not demo.demo:
        argv = argv + ['--dataset', str(tmp_path / 'dataset')]
    run_tool(front, tool, argv)
    assert_clean(front)
    assert not [script for script in front.scripts if '\n\n' in script]
    return ''.join(re.sub(r'CHOPPER-\d+-\d+', 'CHOPPER-<token>', script) + '\n\n'
                   for script in front.scripts)


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', PRINTERS)
@pytest.mark.parametrize('run', RUNS)
def test_a_single_motor_gets_the_scripts_main_sent(source, printer, run, tmp_path, monkeypatch):
    require(source)
    with open(os.path.join(REFERENCE, '%s-%s.txt' % (run, printer))) as reference:
        assert sent(source, printer, run, tmp_path, monkeypatch) == reference.read()
