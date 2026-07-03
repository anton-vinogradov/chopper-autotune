"""Command line: `collect` gathers a dataset on the printer host, `analyze` works offline."""
from __future__ import annotations

import argparse
import re
import sys

from .collect import Range

_STORE_TRUE = {'--no-raw', '--dry-run', '--yes', '--csv', '--skip-audible', '--recompute',
               '--no-html', '--apply', '--save'}


def _gcode_args(argv: 'list[str]') -> 'list[str]':
    """Translate Klipper-style KEY=VALUE params (as passed by RUN_SHELL_COMMAND) into CLI flags."""
    out = []
    for arg in argv:
        match = re.fullmatch(r'([A-Za-z][A-Za-z0-9_]*)=(.*)', arg)
        if match is None:
            out.append(arg)
            continue
        flag = '--' + match.group(1).lower().replace('_', '-')
        if flag in _STORE_TRUE:
            if match.group(2).lower() in ('1', 'true', 'yes'):
                out.append(flag)
        else:
            out.extend((flag, match.group(2)))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='chopper-autotune',
                                     description='Measurement-driven tuning of TMC chopper registers for Klipper')
    sub = parser.add_subparsers(dest='command', required=True)

    u = sub.add_parser('tune', help='the whole pipeline in one command: resonance speed + descent per axis')
    u.add_argument('--axis', type=str.lower, choices=('x', 'y', 'xy'), default='xy',
                   help='default xy: both axes, the second seeded with the first winner')
    u.add_argument('--speed', type=Range.parse, default=None,
                   help='skip the resonance scan and use this speed (mm/s)')
    u.add_argument('--save', action='store_true',
                   help='write the winners into the Klipper config (with backups) and restart')
    u.add_argument('--iterations', type=int, default=1)
    u.add_argument('--audible-weight', type=float, default=0.25)
    u.add_argument('--accel', type=float, default=None)
    u.add_argument('--no-raw', action='store_true')
    u.add_argument('--csv', action='store_true')
    u.add_argument('--socket', default=None)
    u.add_argument('--url', default='http://127.0.0.1:7125')
    u.add_argument('--dry-run', action='store_true')

    c = sub.add_parser('collect', help='run measurements on the printer, write a dataset')
    c.add_argument('--socket', default=None,
                   help='klippy unix socket path, default: auto-detect (printer_data/comms, /tmp/klippy_uds)')
    c.add_argument('--csv', action='store_true',
                   help='fallback capture via ACCELEROMETER_MEASURE and /tmp CSV instead of streaming')
    c.add_argument('--axis', type=str.lower, choices=('x', 'y'), default='x')
    c.add_argument('--speed', type=Range.parse, required=True,
                   help='speed or range in mm/s, e.g. 55 or 40:70 (resonance speed of the axis)')
    c.add_argument('--tbl', type=Range.parse, default=Range(0, 3), help='blank time range, default 0:3')
    c.add_argument('--toff', type=Range.parse, default=Range(1, 8), help='slow decay range, default 1:8')
    c.add_argument('--hstrt', type=Range.parse, default=Range(0, 7), help='hysteresis start range, default 0:7')
    c.add_argument('--hend', type=Range.parse, default=Range(0, 15), help='hysteresis end range, default 0:15')
    c.add_argument('--tpfd', type=Range.parse, default=None, help='TPFD range (TMC2240/5160 only)')
    c.add_argument('--search', choices=('grid', 'descent'), default='grid',
                   help='grid = full sweep; descent = coordinate descent per AN-001, minutes instead of hours')
    c.add_argument('--audible-weight', type=float, default=0.25,
                   help='descent objective penalty for audible chopper frequency')
    c.add_argument('--seed-from', default=None,
                   help='start the descent from the best config of a previous dataset '
                        '(fast second axis: every candidate is still measured on this one)')
    c.add_argument('--iterations', type=int, default=1, help='repeats per combination, default 1')
    c.add_argument('--skip-audible', action='store_true',
                   help='exclude combinations with an audible chopper frequency instead of just penalizing them')
    c.add_argument('--measure-time', type=float, default=1.25, help='cruise time per move in seconds')
    c.add_argument('--accel', type=float, default=None, help='acceleration, default printer max_accel / 10')
    c.add_argument('--trim', type=float, default=None,
                   help='guard fraction of the cruise window (stream, default 0.1); '
                        'with --csv: fraction of the whole capture (default 0.25)')
    c.add_argument('--dataset', default=None, help='dataset directory; pass an existing one to resume')
    c.add_argument('--no-raw', action='store_true',
                   help='do not keep raw accelerometer csv (disables analyze --recompute)')
    c.add_argument('--dry-run', action='store_true', help='print the plan and ETA, do not move anything')
    c.add_argument('-y', '--yes', action='store_true', help='skip the confirmation prompt')

    f = sub.add_parser('find-speed', help='sweep speeds with current registers to locate resonance peaks')
    f.add_argument('--socket', default=None,
                   help='klippy unix socket path, default: auto-detect')
    f.add_argument('--csv', action='store_true',
                   help='fallback capture via ACCELEROMETER_MEASURE and /tmp CSV instead of streaming')
    f.add_argument('--axis', type=str.lower, choices=('x', 'y'), default='x')
    f.add_argument('--min-speed', type=int, default=20)
    f.add_argument('--max-speed', type=int, default=120)
    f.add_argument('--step', type=int, default=2, help='speed increment in mm/s, default 2')
    f.add_argument('--iterations', type=int, default=1)
    f.add_argument('--measure-time', type=float, default=1.0,
                   help='target cruise time per move; shrinks at high speeds to fit the axis')
    f.add_argument('--accel', type=float, default=None, help='acceleration, default printer max_accel / 10')
    f.add_argument('--trim', type=float, default=None)
    f.add_argument('--dataset', default=None, help='dataset directory; pass an existing one to resume')
    f.add_argument('--no-raw', action='store_true')
    f.add_argument('--dry-run', action='store_true')
    f.add_argument('-y', '--yes', action='store_true')

    s = sub.add_parser('simulate', help='replay the descent strategy against a recorded grid dataset')
    s.add_argument('dataset')
    s.add_argument('--audible-weight', type=float, default=0.25)

    t = sub.add_parser('status', help='progress of the most recent (or given) dataset')
    t.add_argument('dataset', nargs='?', default=None)
    t.add_argument('--total', type=int, default=None, help='planned moves, for ETA of pre-ranges datasets')

    m = sub.add_parser('compare', help='agreement between two datasets: winners, rank correlation, top overlap')
    m.add_argument('dataset_a')
    m.add_argument('dataset_b')
    m.add_argument('--top', type=int, default=10)
    m.add_argument('--audible-weight', type=float, default=0.25)

    a = sub.add_parser('analyze', help='rank configurations from a dataset, report, optionally apply')
    a.add_argument('dataset', nargs='?', default=None,
                   help='dataset directory, default: the latest collected one')
    a.add_argument('--top', type=int, default=15, help='rows in the console table')
    a.add_argument('--audible-weight', type=float, default=0.25,
                   help='score penalty for chopper frequency in the audible range')
    a.add_argument('--trim', type=float, default=0.25)
    a.add_argument('--recompute', action='store_true', help='recompute metrics from raw csv')
    a.add_argument('--html', default=None, help='report path, default <dataset>/report.html')
    a.add_argument('--no-html', action='store_true')
    a.add_argument('--apply', action='store_true', help='apply the best config via SET_TMC_FIELD')
    a.add_argument('--save', action='store_true',
                   help='persist the best config into the Klipper config file and restart Klipper')
    a.add_argument('--url', default='http://127.0.0.1:7125')

    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    args = parser.parse_args(_gcode_args(sys.argv[1:] if argv is None else argv))
    if args.command == 'tune':
        from .tune import run_tune
        return run_tune(args)
    if args.command == 'collect':
        from .collect import run_collect
        return run_collect(args)
    if args.command == 'find-speed':
        from .find_speed import run_find_speed
        return run_find_speed(args)
    if args.command == 'simulate':
        from .search import run_simulate
        return run_simulate(args)
    if args.command == 'compare':
        from .analyze import run_compare
        return run_compare(args)
    if args.command == 'status':
        from .analyze import run_status
        return run_status(args)
    from .analyze import run_analyze
    return run_analyze(args)
