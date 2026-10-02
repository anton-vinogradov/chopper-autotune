"""A run pops up once, at its end: an M118 (`echo:`, a KlipperScreen popup) while the
motors still have work covers the panel mid-run."""
import chopper_autotune.collect as col
from chopper_autotune import tune
from chopper_autotune.cli import build_parser


def popups_after_the_last_move(log: 'list[str]') -> 'tuple[int, int]':
    last_move = max(index for index, line in enumerate(log) if line == 'MOVE')
    popups = [index for index, line in enumerate(log) if line.startswith('M118')]
    return len(popups), sum(index > last_move for index in popups)


def test_a_tune_pops_up_once_at_its_end(stub_printer):
    tune.run_tune(build_parser().parse_args(['tune', '--motor', 'ab', '--no-raw']))
    assert popups_after_the_last_move(stub_printer.log) == (1, 1)


def test_a_grid_collect_pops_up_once_after_its_validation(stub_printer, tmp_path):
    args = build_parser().parse_args(['collect', '--axis', 'x', '--speed', '60', '--tbl', '0:0',
                                      '--toff', '3:5', '--hstrt', '3:3', '--hend', '5:6',
                                      '--yes', '--no-raw', '--dataset', str(tmp_path / 'grid')])
    col.collect(stub_printer.kl(), args)
    assert popups_after_the_last_move(stub_printer.log) == (1, 1)
