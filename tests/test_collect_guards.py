import pytest

from chopper_autotune.collect import check_resume, refuse_if_printing, run_restore
from chopper_autotune.klippy import KlippyError


def test_run_restore_runs_every_step(capsys):
    # a failing step (or a second SIGTERM mid-restore) must not cancel the rest:
    # registers, spreadCycle and homing each get their chance
    order = []

    def boom():
        raise SystemExit(143)

    run_restore(lambda: order.append('registers'), boom, lambda: order.append('home'))
    assert order == ['registers', 'home']
    assert 'restore step failed' in capsys.readouterr().out


class FakeKl:
    def __init__(self, state):
        self.state = state

    def is_printing(self):
        if isinstance(self.state, Exception):
            raise self.state
        return self.state


def test_refuse_if_printing():
    refuse_if_printing(FakeKl(False))
    with pytest.raises(SystemExit, match='busy printing'):
        refuse_if_printing(FakeKl(True))
    refuse_if_printing(FakeKl(KlippyError('no print_stats')))   # no [virtual_sdcard]: allow


def test_check_resume_rejects_different_conditions():
    manifest = {'speeds': [58], 'accel': 300.0, 'measure_time': 1.25}
    check_resume(manifest, [58], 300.0, 1.25)
    check_resume({}, [58], 300.0, 1.25)          # pre-key dataset: nothing to compare
    with pytest.raises(SystemExit, match='measure_time'):
        check_resume(manifest, [58], 300.0, 0.4)
    with pytest.raises(SystemExit, match='speeds'):
        check_resume(manifest, [40, 58], 300.0, 1.25)
