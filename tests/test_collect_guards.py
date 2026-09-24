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


def test_report_winner_reports_improvement_vs_defaults(tmp_path, monkeypatch, capsys):
    import json
    from types import SimpleNamespace

    from chopper_autotune import dataset as dataset_mod
    from chopper_autotune import tmc
    from chopper_autotune.collect import report_winner
    from chopper_autotune.dataset import Dataset

    monkeypatch.setattr(dataset_mod, 'RESULTS_HOME', tmp_path)
    ds = Dataset.create(tmp_path / 'ds', {'mode': 'test'})
    for combo, magnitude in ((tmc.KLIPPER_DEFAULT, 2000.0), (tmc.Chopper(0, 2, 4, 7), 1000.0)):
        for direction in (1, -1):
            ds.append({'id': '%s_%d' % (combo.label(), direction), 'kind': 'move',
                       'status': 'ok', **combo.fields(), 'tpfd': None,
                       'score': {'median_magnitude': magnitude, 'clicks': 0}})

    finals = []
    hw = SimpleNamespace(driver=tmc.DRIVERS['2209'], stepper='stepper_x')
    args = SimpleNamespace(trim=0.1, audible_weight=0.25)
    screen = SimpleNamespace(final=finals.append)
    winner = report_winner(hw, ds, args, screen, top=5)

    assert winner['chopper'] == tmc.Chopper(0, 2, 4, 7)
    assert ds.manifest()['improvement'] == 2.0
    assert 'less vibration' in finals[0]                 # the display says what it bought
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['x'] == {'regs': '0/2/4/7', 'quieter': 2.0}   # the panel column fills


def test_resolve_accel_chip_never_guesses_a_name():
    import pytest

    from chopper_autotune.collect import resolve_accel_chip
    assert resolve_accel_chip({'resonance_tester': {'accel_chip': 'adxl345 hotend'}}, 'x') \
        == 'adxl345 hotend'
    # two-chip setups name the chip per axis
    two = {'resonance_tester': {'accel_chip_x': 'adxl345 head', 'accel_chip_y': 'adxl345 bed'}}
    assert resolve_accel_chip(two, 'x') == 'adxl345 head'
    assert resolve_accel_chip(two, 'y') == 'adxl345 bed'
    # no [resonance_tester]: the single accelerometer section is unambiguous
    assert resolve_accel_chip({'adxl345 hotend': {}, 'printer': {}}, 'x') == 'adxl345 hotend'
    with pytest.raises(SystemExit, match='no accelerometer'):
        resolve_accel_chip({'printer': {}}, 'x')
    with pytest.raises(SystemExit, match='several'):
        resolve_accel_chip({'adxl345': {}, 'lis2dw bed': {}}, 'x')
    assert resolve_accel_chip({'bmi160': {}, 'printer': {}}, 'x') == 'bmi160'


def test_resolve_accel_chip_reads_kalicos_accel_chips():
    import pytest

    from chopper_autotune.collect import resolve_accel_chip
    # Kalico reads accel_chips before accel_chip_x/y and accel_chip
    one = {'resonance_tester': {'accel_chips': ' lis2dw ', 'accel_chip': 'adxl345'}}
    assert resolve_accel_chip(one, 'x') == 'lis2dw'
    # several measure together: the per-axis names say which one each motor moves
    several = {'resonance_tester': {'accel_chips': 'adxl345 head, adxl345 bed'}}
    with pytest.raises(SystemExit, match=r'several \(adxl345 head, adxl345 bed\).*accel_chip_x'):
        resolve_accel_chip(several, 'x')
    # Kalico reads accel_chip beside several accel_chips without using it: a leftover
    several['resonance_tester']['accel_chip'] = 'adxl345'
    with pytest.raises(SystemExit, match='several'):
        resolve_accel_chip(several, 'x')
    several['resonance_tester'].update(accel_chip_x='adxl345 head', accel_chip_y='adxl345 bed')
    assert resolve_accel_chip(several, 'y') == 'adxl345 bed'


def test_a_beacon_accelerometer_is_named_not_guessed():
    from chopper_autotune.collect import resolve_accel_chip
    assert resolve_accel_chip({'resonance_tester': {'accel_chip': 'beacon'}}, 'x') == 'beacon'
    # only a Beacon RevH has one: a [beacon] section alone is not an accelerometer
    with pytest.raises(SystemExit, match='no accelerometer'):
        resolve_accel_chip({'beacon': {}, 'beacon model default': {}, 'printer': {}}, 'x')


@pytest.mark.parametrize('chip, settings, command_chip', [
    ('adxl345', {}, 'adxl345'),
    ('adxl345 hotend', {}, 'hotend'),
    ('beacon', {'beacon': {'home_z_hop': 5.0}}, 'beacon'),
    ('beacon', {'beacon': {'accel_name': 'probe'}}, 'probe'),
    ('beacon sensor tool', {'beacon sensor tool': {'accel_name': 'beacon_tool'}}, 'beacon_tool'),
    ('beacon sensor tool', {}, 'beacon_tool'),
])
def test_the_chip_name_accelerometer_measure_takes(chip, settings, command_chip):
    from chopper_autotune.collect import accel_command_chip
    assert accel_command_chip(settings, chip) == command_chip


def test_kalicos_limited_corexy_is_coupled():
    from chopper_autotune.collect import coupled_xy
    assert coupled_xy('limited_corexy') and coupled_xy('corexy')
    assert not coupled_xy('limited_cartesian') and not coupled_xy('cartesian')


def test_full_steps_per_mm_honours_gearing():
    from chopper_autotune.collect import full_steps_per_mm, gear_factor
    assert gear_factor(None) == 1.0
    assert gear_factor('80:16') == 5.0
    assert gear_factor([[80.0, 16.0]]) == 5.0            # as Klipper keeps it in settings
    assert gear_factor('80:16, 2:1') == 10.0
    # the project's own anchor: rotation 40 -> 5 full steps/mm, 290 fs/s = 58 mm/s
    assert full_steps_per_mm({'rotation_distance': 40}) == 5.0
    assert 290 / full_steps_per_mm({'rotation_distance': 40}) == pytest.approx(58.0)
    # a belted Z (80:16 on a 40 mm pulley) is 8 mm/turn like a T8x8 screw
    assert full_steps_per_mm({'rotation_distance': 40, 'gear_ratio': [[80, 16]]}) == 25.0
    assert full_steps_per_mm({'rotation_distance': 8, 'full_steps_per_rotation': 400}) == 50.0


@pytest.mark.parametrize('section, options, command_chip', [
    ('adxl345 hotend', {}, 'hotend'),                     # the name word, not the section
    ('beacon', {'accel_name': 'probe'}, 'probe'),         # Beacon's name for its chip
])
def test_capture_csv_names_the_chip_as_klipper_registered_it(tmp_path, monkeypatch, section,
                                                             options, command_chip):
    import chopper_autotune.collect as collect

    class FakeKl:
        def __init__(self):
            self.scripts = []

        def settings(self):
            return {'printer': {'kinematics': 'corexy', 'max_accel': 10000},
                    'stepper_x': {'position_min': 0, 'position_max': 260},
                    'stepper_y': {'position_min': 0, 'position_max': 260},
                    'tmc2209 stepper_x': {}, 'resonance_tester': {'accel_chip': section},
                    section: options}

        def object_list(self):
            return []

        def gcode(self, script):
            self.scripts.append(script)

    csv = tmp_path / 'chip-v060.csv'
    csv.write_text('#time,x,y,z\n' + ''.join('%.4f,0,0,0\n' % (t / 100) for t in range(50)))
    monkeypatch.setattr(collect, 'drop_stale_csv', lambda name: None)
    monkeypatch.setattr(collect, 'wait_for_csv', lambda name, span: str(csv))
    kl = FakeKl()
    collect.capture_csv(collect.detect_hardware(kl, 'x'), 'v060', 'G4 P100')
    assert 'ACCELEROMETER_MEASURE CHIP=%s NAME=v060\n' % command_chip in kl.scripts[0]


def test_report_winner_finds_the_stock_reference_when_tpfd_is_not_swept(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    from chopper_autotune import dataset as dataset_mod
    from chopper_autotune import tmc
    from chopper_autotune.collect import report_winner
    from chopper_autotune.dataset import Dataset

    monkeypatch.setattr(dataset_mod, 'RESULTS_HOME', tmp_path)
    ds = Dataset.create(tmp_path / 'ds', {'mode': 'test'})
    # a grid without --tpfd on a TMC2240 spells every combo with tpfd=None
    for combo, magnitude in ((tmc.Chopper(2, 3, 5, 2), 2000.0), (tmc.Chopper(0, 2, 4, 7), 1000.0)):
        for direction in (1, -1):
            ds.append({'id': '%s_%d' % (combo.label(), direction), 'kind': 'move',
                       'status': 'ok', **combo.fields(), 'tpfd': None,
                       'score': {'median_magnitude': magnitude, 'clicks': 0}})
    hw = SimpleNamespace(driver=tmc.DRIVERS['2240'], stepper='stepper_x', baseline={})
    args = SimpleNamespace(trim=0.1, audible_weight=0.25, tpfd=None)
    report_winner(hw, ds, args, SimpleNamespace(final=lambda text: None), top=5)
    assert ds.manifest()['improvement'] == 2.0
    # the panel reads the fifth (TPFD) register from the config or Klipper's stock value
    state = json.loads((tmp_path / 'state.json').read_text())
    assert state['x']['regs'] == '0/2/4/7/4'


def test_shown_registers_carry_the_configs_tpfd():
    from types import SimpleNamespace

    from chopper_autotune import tmc
    from chopper_autotune.collect import shown_registers
    hw = SimpleNamespace(driver=tmc.DRIVERS['2240'], baseline={'tpfd': 7})
    assert shown_registers(hw, tmc.Chopper(0, 2, 4, 7)) == tmc.Chopper(0, 2, 4, 7, 7)
    assert shown_registers(hw, tmc.Chopper(0, 2, 4, 7, 1)) == tmc.Chopper(0, 2, 4, 7, 1)
    hw = SimpleNamespace(driver=tmc.DRIVERS['2209'], baseline={})
    assert shown_registers(hw, tmc.Chopper(0, 2, 4, 7)) == tmc.Chopper(0, 2, 4, 7)


def test_endstop_tools_need_no_accelerometer():
    import pytest

    from chopper_autotune.collect import detect_hardware

    class FakeKl:
        def __init__(self, settings):
            self._settings = settings

        def settings(self):
            return self._settings

        def object_list(self):
            return []

    settings = {'printer': {'kinematics': 'corexy', 'max_accel': 10000},
                'stepper_x': {'position_min': 0, 'position_max': 260},
                'stepper_y': {'position_min': 0, 'position_max': 260},
                'tmc2209 stepper_x': {}}
    # CHOPPER_CURRENT / CHOPPER_ENVELOPE judge by the endstop and never stream
    assert detect_hardware(FakeKl(settings), 'x', accel=False).accel_chip == ''
    with pytest.raises(SystemExit, match='accelerometer'):
        detect_hardware(FakeKl(settings), 'x')


def test_mid_run_rehome_keeps_the_motors_energized():
    from types import SimpleNamespace

    from chopper_autotune.collect import PARK_INTERVAL_MOVES, make_parker, park
    scripts = []
    kl = SimpleNamespace(gcode=scripts.append, settings=lambda: {},
                         stepper_states=lambda: {'stepper_x': True, 'stepper_y': True,
                                                 'stepper_z': True, 'extruder': True})
    hw = SimpleNamespace(center=(130.0, 130.0), axis_span=260.0)
    park(kl, hw)                                   # the start: all but Z off for the noise floor
    for name in ('stepper_x', 'stepper_y', 'extruder'):
        assert 'SET_STEPPER_ENABLE STEPPER="%s" ENABLE=0' % name in scripts[-1]
    assert 'stepper_z' not in scripts[-1]          # Z keeps its homing (safe_z_home z_hop)
    assert 'M18' not in scripts[-1]
    before_move = make_parker(kl, hw)
    for _ in range(PARK_INTERVAL_MOVES + 1):
        before_move(1, 1.0)
    # a disable->enable mid-run would reset toff (Klipper's config copy or klipper_tmc_autotune)
    assert 'G28 X Y' in scripts[-1] and 'M18' not in scripts[-1]


def test_process_start_reads_linux_proc(tmp_path, monkeypatch):
    import os

    import chopper_autotune.collect as collect_mod
    monkeypatch.setattr(collect_mod.time, 'time', lambda: 1700000500.25)
    (tmp_path / '4242').mkdir()
    # a command name may hold spaces and ')': the fields count from the last ')'
    (tmp_path / '4242' / 'stat').write_text('4242 (py) klippy) S 1 ' + '0 ' * 17 + '12345 0 0\n')
    (tmp_path / 'uptime').write_text('500.25 1900.00\n')     # seconds since boot, to 10 ms
    assert collect_mod.process_start(4242, str(tmp_path)) == pytest.approx(
        1700000000 + 12345 / os.sysconf('SC_CLK_TCK'))
    assert collect_mod.process_start(4243, str(tmp_path)) is None
    assert collect_mod.process_start(None, str(tmp_path)) is None   # v0.10, v0.11: no process_id


def test_process_start_of_this_process():
    import os
    import time

    from chopper_autotune.collect import process_start
    if not os.path.exists('/proc/self/stat'):
        pytest.skip('no /proc here (Linux only)')
    started = process_start(os.getpid())
    assert time.time() - 3600 < started <= time.time() + 1


@pytest.mark.parametrize('mtime, process_id, trusted', [
    (1000, 7, True),        # the process started after the file was written
    (3000, 7, False),       # a git pull without a service restart: older code may run
    (1000, None, False),    # v0.10, v0.11: the process is unknown
])
def test_klipper_extra_trusts_only_code_older_than_the_process(tmp_path, monkeypatch,
                                                               mtime, process_id, trusted):
    import os
    from types import SimpleNamespace

    import chopper_autotune.collect as collect_mod
    monkeypatch.setattr(collect_mod, '_KLIPPER_EXTRAS', {})
    monkeypatch.setattr(collect_mod, 'process_start', lambda pid: {7: 2000.0}.get(pid))
    (tmp_path / 'klippy' / 'extras').mkdir(parents=True)
    for name, text in (('force_move.py', 'CLEAR_HOMED'), ('resonance_tester.py', 'CHIPS')):
        path = tmp_path / 'klippy' / 'extras' / name
        path.write_text(text)
        os.utime(path, (mtime, mtime))
    kl = SimpleNamespace(info=lambda: {'klipper_path': str(tmp_path), 'process_id': process_id})
    assert collect_mod.klipper_extra(kl, 'force_move.py') == ('CLEAR_HOMED' if trusted else '')
    assert collect_mod.can_clear_homing(kl) is trusted
    # a question older code answers the same way reads the file whatever its age
    assert collect_mod.klipper_extra(kl, 'resonance_tester.py', any_age=True) == 'CHIPS'
    assert collect_mod.klipper_extra(kl, 'missing.py', any_age=True) == ''
