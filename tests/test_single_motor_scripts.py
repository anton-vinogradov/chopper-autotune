"""The scripts a printer with one motor per axis gets from COLLECT, FIND_SPEED, DEMO, MAP
and TUNE, what the run prints and the dataset it writes, recorded from the code of main:
tools that learn to move a rail (#129) must not change a line for a single motor. The
printers are AWD's own (tests/test_klipper_front.py) without the twins, frozen beside the
scripts (printer-*.cfg): meijjaa's cartesian with its low limits, a Voron's CoreXY, and that
Voron with a second motor on Y alone, where motor A still runs alone on its axis; each built
from every release's modules. The scores the text and the dataset carry come from the test
printer's accelerometer, whose samples fall a little apart on each release: they are held on
PRINTED_ON, to nine significant digits, the last ones being numpy's and the platform's.

After merging main, record the references again from main's code:

    python tests/record_single_motor_scripts.py --check
    python tests/record_single_motor_scripts.py origin/main

The first command records from the commit the references came from, which
single_motor_scripts/recorded-from names, with the merged tests. It must reproduce them byte
for byte, except a score's digits past the ninth significant one, which this test ignores. If
the recording fails or more differs, the merge changed how the tests drive the tools (run(),
the test printer), and a new recording would carry that change along with main's: find out why
first. The second command records from main into single_motor_scripts and names the files that
changed. Apart from such digits, each change in `git diff tests/single_motor_scripts` must
come from a change in main; this test then holds the merged code to them. A normal test run
records nothing."""
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
    # stepper_x warns of heat after the 3rd move: the stop releases the gantry, each X/Y
    # motor switched on and off (rehome_unless_hot)
    'collect-hot': (collect.collect, COLLECT + ['--axis', 'x', '--speed', '60']),
    'find-speed': (scan, ['find-speed', '--axis', 'x', '--min-speed', '30', '--max-speed', '90',
                          '--step', '10', '--yes', '--no-raw']),
    'find-speed-b': (scan, ['find-speed', '--axis', 'y', '--min-speed', '30', '--max-speed', '90',
                            '--step', '10', '--yes', '--no-raw']),
    'demo': (demo.demo, ['demo', '--axis', 'x', '--speed', '60', '--report', '--iterations', '1']),
    'demo-b': (demo.demo, ['demo', '--axis', 'y', '--speed', '40', '--report', '--iterations', '1']),
    'demo-live': (demo.demo, ['demo', '--axis', 'x', '--speed', '60', '--rounds', '1',
                              '--repeats', '1']),
    'demo-ab': (demo.showcase_together, ['demo', '--rounds', '1', '--repeats', '1']),
    'map': (resonance_map.resonance_map, ['map', '--axis', 'x', '--min-speed', '40', '--max-speed',
                                          '60', '--step', '10', '--yes', '--no-raw']),
    # both motors, B seeded by A's winner, each scan finding 60 mm/s (run); on one printer:
    # a descent is long
    'tune': (tune.run_tune, ['tune', '--no-raw']),
}
PARK_INTERVAL = {'collect-rehome': 3}
HOT_AFTER = {'collect-hot': 3}
# the show of both motors takes no SPEED=: each motor plays at the speed of its last tune
TUNED_AT = {'demo-ab': 60}
PRINTERS = ('meijjaa', 'voron-2209', 'dual-y-voron-2209')
# on the dual-Y Voron motor B is a rail, not a single motor, and the show of both runs it
CASES = [(run, printer) for run in RUNS for printer in PRINTERS
         if (run != 'tune' or printer == 'voron-2209')
         and not (printer.startswith('dual-y') and run.endswith(('-b', '-ab')))]
DATASET = ('manifest.json', 'measurements.jsonl')
PRINTED_ON = 'klipper-v0.13.0'


def nine_digits(text: str) -> str:
    return re.sub(r'\d+\.\d{7,}', lambda m: '%.9g' % float(m.group()), text)


class PrinterClock(klipper_front.Clock):
    """The tools' clock on the printer's time: the display's pace and an ETA come out the
    same on every run."""

    def __init__(self, front):
        self.front = front

    def monotonic(self):
        return self.front.printer.reactor.monotonic()

    time = monotonic


def warn_hot_after(front, moves: int):
    """The driver of stepper_x warns of over-temperature once `moves` FORCE_MOVEs ran."""
    chip = next(chip for section, chip in front.chips.items() if section.endswith(' stepper_x'))
    force_move = front.printer.objects['force_move']
    real_move = force_move.manual_move

    def manual_move(*args):
        real_move(*args)
        if len(front.moves) == moves:
            chip.reads['DRV_STATUS'] = chip.fields.all_fields['DRV_STATUS']['otpw']
    force_move.manual_move = manual_move


def run(source: str, printer: str, case: str, tmp_path, monkeypatch, capsys) -> 'tuple[str, str]':
    """The scripts of a run, a blank line after each; and what it printed, then its
    dataset's files, each under a '=== name ===' line. The console fence's token (the
    process id) as <token>; the run's paths and times as <tmp>, <time> and <stamp>."""
    monkeypatch.setattr(klipper_front, 'CLOCK', [0.])
    with open(os.path.join(REFERENCE, 'printer-%s.cfg' % printer)) as cfg:
        front = klipper_front.Front(source, cfg.read())
    monkeypatch.setattr(collect, 'time', PrinterClock(front))
    monkeypatch.setattr(collect, 'PARK_INTERVAL_MOVES', PARK_INTERVAL.get(case, 400))
    if case in HOT_AFTER:
        warn_hot_after(front, HOT_AFTER[case])
    if case in TUNED_AT:
        monkeypatch.setattr(demo, 'known_speed', lambda axis: TUNED_AT[case])
    tool, argv = RUNS[case]
    capsys.readouterr()
    if tool is tune.run_tune:
        kl = front.connect()
        monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
        monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
        monkeypatch.setattr(tune, 'scan', lambda kl, args, popup=True: (0, 60, 60))
        tool(build_parser().parse_args(argv))
    else:
        if tool not in (demo.demo, demo.showcase_together):
            argv = argv + ['--dataset', str(tmp_path / 'dataset')]
        if case in HOT_AFTER:
            with pytest.raises(collect.DriverTooHot):
                run_tool(front, tool, argv)
        else:
            run_tool(front, tool, argv)
    assert_clean(front)
    assert not [script for script in front.scripts if '\n\n' in script]
    scripts = ''.join(re.sub(r'CHOPPER-\d+-\d+', 'CHOPPER-<token>', script) + '\n\n'
                      for script in front.scripts)
    printed = capsys.readouterr().out
    for name in DATASET:
        path = tmp_path / 'dataset' / name
        if path.exists():
            printed += '=== %s ===\n%s' % (name, path.read_text())
    printed = re.sub(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\+00:00', '<time>',
                     printed.replace(str(tmp_path), '<tmp>'))
    return scripts, re.sub(r'\d{8}_\d{6}', '<stamp>', re.sub(r'CHOPPER-\d+-\d+', 'CHOPPER-<token>',
                                                             printed))


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('case, printer', CASES)
def test_a_single_motor_sends_and_prints_what_main_did(source, printer, case, tmp_path,
                                                       monkeypatch, capsys):
    require(source)
    scripts, printed = run(source, printer, case, tmp_path, monkeypatch, capsys)
    with open(os.path.join(REFERENCE, '%s-%s.txt' % (case, printer))) as reference:
        assert scripts == reference.read()
    if source == PRINTED_ON:
        with open(os.path.join(REFERENCE, '%s-%s.out' % (case, printer))) as reference:
            assert nine_digits(printed) == nine_digits(reference.read())
