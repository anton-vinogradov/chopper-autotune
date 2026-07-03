"""Command line: `collect` gathers a dataset on the printer host, `analyze` works offline."""
from __future__ import annotations

import argparse
import re
import sys

from .collect import Range

_STORE_TRUE = {'--no-raw', '--dry-run', '--yes', '--csv', '--recompute', '--no-html', '--apply'}


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
    c.add_argument('--iterations', type=int, default=1, help='repeats per combination, default 1')
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
    a.add_argument('--url', default='http://127.0.0.1:7125')

    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    args = parser.parse_args(_gcode_args(sys.argv[1:] if argv is None else argv))
    if args.command == 'collect':
        from .collect import run_collect
        return run_collect(args)
    if args.command == 'find-speed':
        from .find_speed import run_find_speed
        return run_find_speed(args)
    from .analyze import run_analyze
    return run_analyze(args)
