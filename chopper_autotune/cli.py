"""Command line: `collect` gathers a dataset on the printer host, `analyze` works offline."""
from __future__ import annotations

import argparse
import re
import signal
import sys

from . import tmc
from .collect import Range


def _chopper(text: str) -> tmc.Chopper:
    parts = [int(v) for v in text.replace('/', ',').split(',')]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError('expected tbl,toff,hstrt,hend, e.g. 2,3,5,0')
    return tmc.Chopper(*parts)


def _motor(text: str) -> str:
    """A/B name the motors we tune; map them to the underlying stepper axes (a=stepper_x,
    b=stepper_y). x/y are accepted too so old scripts keep working."""
    return {'a': 'x', 'b': 'y', 'ab': 'xy'}.get(text.lower(), text.lower())


def _gcode_args(argv: 'list[str]', boolean_flags: 'frozenset[str]') -> 'list[str]':
    """Translate Klipper-style KEY=VALUE params (as passed by RUN_SHELL_COMMAND) into CLI flags."""
    out = []
    for arg in argv:
        match = re.fullmatch(r'([A-Za-z][A-Za-z0-9_]*)=(.*)', arg)
        if match is None:
            out.append(arg)
            continue
        flag = '--' + match.group(1).lower().replace('_', '-')
        if flag in boolean_flags:
            value = match.group(2).lower()
            if value in ('1', 'true', 'yes', 'on', 'y'):
                out.append(flag)
            elif value not in ('0', 'false', 'no', 'off', 'n', ''):
                # silently treating e.g. DRY_RUN=Y as "off" would move the printer
                raise SystemExit('%s: expected a boolean like 1/0, got %r'
                                 % (match.group(1), match.group(2)))
        else:
            out.extend((flag, match.group(2)))
    return out


def boolean_flags(parser: argparse.ArgumentParser) -> 'frozenset[str]':
    """All store_true option strings across subcommands, so KEY=1 macro params map to flags."""
    flags = set()
    subactions = [action for action in parser._actions
                  if isinstance(action, argparse._SubParsersAction)]
    for subparser in subactions[0].choices.values():
        for action in subparser._actions:
            if isinstance(action, argparse._StoreTrueAction):
                flags.update(action.option_strings)
    return frozenset(flags)


def install_sigterm_handler():
    """gcode_shell_command timeouts and service restarts send SIGTERM: turn it into
    SystemExit so the finally blocks restore registers, spreadCycle and homing."""
    try:
        signal.signal(signal.SIGTERM, lambda signum, frame: sys.exit(143))
    except ValueError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog='chopper-autotune',
                                     description='Measurement-driven tuning of TMC chopper registers for Klipper')
    sub = parser.add_subparsers(dest='command', required=True)

    u = sub.add_parser('tune', help='the whole pipeline in one command: resonance speed + descent per motor')
    u.add_argument('--motor', '--axis', dest='axis', type=_motor, choices=('x', 'y', 'xy'), default='xy',
                   help='motor a/b/ab (a=stepper_x, b=stepper_y); default ab: both motors, '
                        'the second seeded with the first winner (x/y/xy also accepted)')
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
    c.add_argument('--motor', '--axis', dest='axis', type=_motor, choices=('x', 'y'), default='x',
                   help='motor a or b (a=stepper_x, b=stepper_y); x/y also accepted')
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
    c.add_argument('--validate', type=int, default=3,
                   help='re-measure top N candidates with extra runs before recommending (0 = off)')
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
    f.add_argument('--motor', '--axis', dest='axis', type=_motor, choices=('x', 'y'), default='x',
                   help='motor a or b (a=stepper_x, b=stepper_y); x/y also accepted')
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

    mp = sub.add_parser('map', help='resonance map: vibration vs speed on the current registers '
                                    '(which speeds ring vs stay quiet)')
    mp.add_argument('--socket', default=None, help='klippy unix socket path, default: auto-detect')
    mp.add_argument('--csv', action='store_true',
                    help='fallback capture via ACCELEROMETER_MEASURE and /tmp CSV instead of streaming')
    mp.add_argument('--motor', '--axis', dest='axis', type=_motor, choices=('x', 'y'), default='x',
                    help='motor a or b (a=stepper_x, b=stepper_y); x/y also accepted')
    mp.add_argument('--min-speed', type=int, default=20)
    mp.add_argument('--max-speed', type=int, default=250)
    mp.add_argument('--step', type=int, default=10, help='speed increment in mm/s, default 10')
    mp.add_argument('--print-speed', type=int, default=None,
                    help='your usual print speed (mm/s): flags whether it sits on a resonance '
                         'and names the quieter speeds nearby')
    mp.add_argument('--iterations', type=int, default=1)
    mp.add_argument('--measure-time', type=float, default=1.0,
                    help='target cruise time per move; shrinks at high speeds to fit the axis')
    mp.add_argument('--accel', type=float, default=None,
                    help='acceleration, default printer max_accel / 4 (reaches print speeds)')
    mp.add_argument('--trim', type=float, default=None)
    mp.add_argument('--dataset', default=None, help='dataset directory; pass an existing one to resume')
    mp.add_argument('--no-raw', action='store_true')
    mp.add_argument('--dry-run', action='store_true')
    mp.add_argument('-y', '--yes', action='store_true')

    d = sub.add_parser('demo', help='play the driver defaults against the tuned registers so you can hear the gain')
    d.add_argument('--motor', '--axis', dest='axis', type=_motor, choices=('x', 'y', 'xy'), default='xy',
                   help='motor a/b/ab (a=stepper_x, b=stepper_y); default ab = both; x/y/xy also accepted')
    d.add_argument('--speed', type=Range.parse, default=None, help='resonance speed; auto-detected if omitted')
    d.add_argument('--default', type=_chopper, default=None,
                   help='the "before" config as tbl,toff,hstrt,hend (default: Klipper 2,3,5,0)')
    d.add_argument('--report', action='store_true',
                   help='measured numbers (defaults vs tuned, Nx quieter) instead of the audible showcase')
    d.add_argument('--iterations', type=int, default=3, help='--report: repeats per config, default 3')
    d.add_argument('--live', action='store_true', help=argparse.SUPPRESS)  # back-compat: showcase is the default
    d.add_argument('--rounds', type=int, default=3, help='showcase: alternations of defaults/tuned, default 3')
    d.add_argument('--repeats', type=int, default=4, help='showcase: moves per config per round, default 4')
    d.add_argument('--measure-time', type=float, default=1.0)
    d.add_argument('--accel', type=float, default=None)
    d.add_argument('--trim', type=float, default=None)
    d.add_argument('--socket', default=None)
    d.add_argument('--dry-run', action='store_true')

    s = sub.add_parser('simulate', help='replay the descent strategy against a recorded grid dataset')
    s.add_argument('dataset')
    s.add_argument('--audible-weight', type=float, default=0.25)

    t = sub.add_parser('status', help='progress of the most recent (or given) dataset')
    t.add_argument('dataset', nargs='?', default=None)
    t.add_argument('--dataset', dest='dataset_opt', default=None,
                   help='same as the positional argument, for DATASET= macro params')
    t.add_argument('--total', type=int, default=None, help='planned moves, for ETA of pre-ranges datasets')

    m = sub.add_parser('compare', help='agreement between two datasets: winners, rank correlation, top overlap')
    m.add_argument('dataset_a')
    m.add_argument('dataset_b')
    m.add_argument('--top', type=int, default=10)
    m.add_argument('--audible-weight', type=float, default=0.25)

    a = sub.add_parser('analyze', help='rank configurations from a dataset, report, optionally apply')
    a.add_argument('dataset', nargs='?', default=None,
                   help='dataset directory, default: the latest collected one')
    a.add_argument('--dataset', dest='dataset_opt', default=None,
                   help='same as the positional argument, for DATASET= macro params')
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

    sv = sub.add_parser('save', help='save the latest tuning result for each motor into the config')
    sv.add_argument('--audible-weight', type=float, default=0.25)
    sv.add_argument('--url', default='http://127.0.0.1:7125')

    cur = sub.add_parser('current', help='find the minimal safe run current per motor '
                                         '(worst-case stress, endstop referee)')
    cur.add_argument('--motor', '--axis', dest='axis', type=_motor, choices=('x', 'y', 'xy'),
                     default='xy', help='motor a/b/ab (a=stepper_x, b=stepper_y); default ab')
    cur.add_argument('--margin', type=float, default=2.0,
                     help='recommended current = skip threshold x this margin, default 2.0')
    cur.add_argument('--min-current', type=float, default=0.3,
                     help='search floor in amps, default 0.3')
    cur.add_argument('--resolution', type=float, default=0.05,
                     help='threshold resolution in amps, default 0.05')
    cur.add_argument('--accel', type=float, default=None,
                     help='stress acceleration, default printer max_accel (the worst case)')
    cur.add_argument('--save', action='store_true',
                     help='write the recommended run_current into the config and restart')
    cur.add_argument('--socket', default=None)
    cur.add_argument('--url', default='http://127.0.0.1:7125')
    cur.add_argument('--dry-run', action='store_true')
    cur.add_argument('-y', '--yes', action='store_true')

    env = sub.add_parser('envelope', help='motor torque ceiling: top speed and acceleration '
                                          'before skipped steps, at the configured run current')
    env.add_argument('--motor', '--axis', dest='axis', type=_motor, choices=('x', 'y', 'xy'),
                     default='xy', help='motor a/b/ab (a=stepper_x, b=stepper_y); default ab')
    env.add_argument('--min-speed', type=int, default=150, help='speed ladder start in mm/s, default 150')
    env.add_argument('--max-speed', type=int, default=350, help='speed ladder end in mm/s, default 350')
    env.add_argument('--step', type=int, default=50, help='speed ladder increment in mm/s, default 50')
    env.add_argument('--accel-probe-speed', type=int, default=150,
                     help='fixed speed for the acceleration sweep in mm/s, default 150')
    env.add_argument('--accel', type=float, default=None,
                     help='speed-sweep acceleration and accel-ladder base, default printer max_accel')
    env.add_argument('--socket', default=None)
    env.add_argument('--dry-run', action='store_true')
    env.add_argument('-y', '--yes', action='store_true')
    return parser


def main(argv=None) -> int:
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(line_buffering=True)
    install_sigterm_handler()

    parser = build_parser()
    args = parser.parse_args(_gcode_args(sys.argv[1:] if argv is None else argv,
                                         boolean_flags(parser)))
    if getattr(args, 'dataset_opt', None):
        args.dataset = args.dataset_opt

    if args.command == 'save':
        from .analyze import run_save_latest
        return run_save_latest(args)
    if args.command == 'current':
        from .current import run_current_tune
        return run_current_tune(args)
    if args.command == 'envelope':
        from .envelope import run_envelope
        return run_envelope(args)
    if args.command == 'tune':
        from .tune import run_tune
        return run_tune(args)
    if args.command == 'collect':
        from .collect import run_collect
        return run_collect(args)
    if args.command == 'find-speed':
        from .find_speed import run_find_speed
        return run_find_speed(args)
    if args.command == 'map':
        from .resonance_map import run_resonance_map
        return run_resonance_map(args)
    if args.command == 'demo':
        from .demo import run_demo
        return run_demo(args)
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
