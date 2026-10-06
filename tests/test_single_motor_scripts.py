"""The scripts a printer with one motor per axis gets from COLLECT, FIND_SPEED, DEMO, MAP
and TUNE, recorded from main before the two-motor rail (#129) and kept as they were: tools
that learn to move a rail must not change a line for a single motor. The printers are
AWD's own (tests/test_klipper_front.py) without the twins, frozen beside the scripts
(printer-*.cfg): meijjaa's cartesian with its low limits, a Voron's CoreXY; each built from
every release's modules."""
import os
import re
import types

import pytest

import klipper_front
from chopper_autotune import collect, demo, resonance_map, tune
from chopper_autotune.cli import build_parser
from chopper_autotune.find_speed import scan
from test_klipper_front import SOURCES, assert_clean, require, run_tool

REFERENCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'single_motor_scripts')
COLLECT = ['collect', '--tbl', '1:1', '--toff', '3:4', '--hstrt', '4:4', '--hend', '3:3',
           '--iterations', '1', '--yes', '--no-raw']
RUNS = {
    'collect': (collect.collect, COLLECT + ['--axis', 'x', '--speed', '60']),
    # meijjaa's max_accel/10 fits 40 mm/s into its Y
    'collect-b': (collect.collect, COLLECT + ['--axis', 'y', '--speed', '40']),
    # a re-home every 3 moves: the one between the moves, on the candidate (make_parker)
    'collect-rehome': (collect.collect, COLLECT + ['--axis', 'x', '--speed', '60']),
    'find-speed': (scan, ['find-speed', '--axis', 'x', '--min-speed', '30', '--max-speed', '90',
                          '--step', '10', '--yes', '--no-raw']),
    'find-speed-b': (scan, ['find-speed', '--axis', 'y', '--min-speed', '30', '--max-speed', '90',
                            '--step', '10', '--yes', '--no-raw']),
    'demo': (demo.demo, ['demo', '--axis', 'x', '--speed', '60', '--report', '--iterations', '1']),
    'demo-b': (demo.demo, ['demo', '--axis', 'y', '--speed', '40', '--report', '--iterations', '1']),
    'demo-live': (demo.demo, ['demo', '--axis', 'x', '--speed', '60', '--rounds', '1',
                              '--repeats', '1']),
    'map': (resonance_map.resonance_map, ['map', '--axis', 'x', '--min-speed', '40', '--max-speed',
                                          '60', '--step', '10', '--yes', '--no-raw']),
    # both motors, the second seeded with the first's winner: on one printer, a descent is long
    'tune': (tune.run_tune, ['tune', '--speed', '60', '--no-raw']),
}
PARK_INTERVAL = {'collect-rehome': 3}
PRINTERS = ('meijjaa', 'voron-2209')
CASES = [(run, printer) for run in RUNS for printer in PRINTERS
         if run != 'tune' or printer == 'voron-2209']


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
    with open(os.path.join(REFERENCE, 'printer-%s.cfg' % printer)) as cfg:
        front = klipper_front.Front(source, cfg.read())
    monkeypatch.setattr(collect, 'time', PrinterClock(front))
    monkeypatch.setattr(collect, 'PARK_INTERVAL_MOVES', PARK_INTERVAL.get(run, 400))
    tool, argv = RUNS[run]
    if tool is tune.run_tune:
        kl = front.connect()
        monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
        monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
        tool(build_parser().parse_args(argv))
    else:
        if tool is not demo.demo:
            argv = argv + ['--dataset', str(tmp_path / 'dataset')]
        run_tool(front, tool, argv)
    assert_clean(front)
    assert not [script for script in front.scripts if '\n\n' in script]
    return ''.join(re.sub(r'CHOPPER-\d+-\d+', 'CHOPPER-<token>', script) + '\n\n'
                   for script in front.scripts)


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('run, printer', CASES)
def test_a_single_motor_gets_the_scripts_main_sent(source, printer, run, tmp_path, monkeypatch):
    require(source)
    with open(os.path.join(REFERENCE, '%s-%s.txt' % (run, printer))) as reference:
        assert sent(source, printer, run, tmp_path, monkeypatch) == reference.read()
