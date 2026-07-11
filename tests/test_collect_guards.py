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


def test_screen_final_adds_a_popup():
    """final() = the status line as usual PLUS one M118 (KlipperScreen popup). Progress
    updates must never popup — mid-run popups cover the panel and its Stop button."""
    from chopper_autotune.collect import Screen

    class FakeKl:
        def __init__(self):
            self.sent = []

        def gcode(self, script):
            self.sent.append(script)

    kl = FakeKl()
    screen = Screen(kl, display=True)
    screen.update('progress 1/10', force=True)
    assert not any(cmd.startswith('M118') for cmd in kl.sent)
    screen.final('Belts matched: A 105 / B 105 Hz')
    assert 'M117 Belts matched: A 105 / B 105 Hz' in kl.sent
    assert 'M118 Belts matched: A 105 / B 105 Hz' in kl.sent


def test_fit_measure_time_shrinks_for_fast_resonances():
    """The measured failure: motor B's 96 mm/s resonance needed 129 mm of travel against
    the 104 mm cap and aborted the tune — the cruise must shrink to fit instead."""
    import pytest

    from chopper_autotune.collect import fit_measure_time

    # 96 mm/s, accel 1000, limit 104 -> fits at ~0.99 s, not the default 1.25
    fitted = fit_measure_time([96], 1000.0, 104.0, 1.25)
    assert 0.9 < fitted < 1.0
    # a comfortable speed keeps the requested cruise
    assert fit_measure_time([58], 1000.0, 104.0, 1.25) == 1.25
    # physically impossible even at the floor -> still a clear error
    with pytest.raises(SystemExit, match='raise --accel'):
        fit_measure_time([250], 1000.0, 104.0, 1.25)


def test_await_flushed_demands_span_and_settled_size(tmp_path):
    import pytest

    from chopper_autotune.collect import await_flushed

    csv = tmp_path / 'adxl345-v060.csv'
    csv.write_text('#time,x,y,z\n' + ''.join('%.4f,0,0,0\n' % (t / 100) for t in range(101)))
    # 1s of data against an expected 4s capture = truncated -> refused
    with pytest.raises(TimeoutError):
        await_flushed(str(tmp_path / '*-v060.csv'), min_span_sec=4.0, timeout=1.0, poll=0.05)
    # the same file against its true duration -> accepted
    assert await_flushed(str(tmp_path / '*-v060.csv'), min_span_sec=1.0,
                         timeout=5.0, poll=0.05) == str(csv)
