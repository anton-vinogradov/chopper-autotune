import os
import time

import pytest

from chopper_autotune.collect import (check_resume, failure_display, refuse_if_printing,
                                      run_restore)
from chopper_autotune.klippy import KlippyError


def test_run_restore_runs_every_step(capsys):
    # a failing step (or a second SIGTERM mid-restore) must not cancel the rest:
    # registers, spreadCycle and homing each get their chance
    order = []

    def boom():
        raise SystemExit(143)

    # a Stop swallowed on a run's success path still ends the run, after every step
    with pytest.raises(SystemExit) as stop:
        run_restore(lambda: order.append('registers'), boom, lambda: order.append('home'))
    assert order == ['registers', 'home'] and stop.value.code == 143
    assert 'restore step failed' in capsys.readouterr().out
    # on the way out already, the exception in flight keeps its own cause
    with pytest.raises(ValueError):
        try:
            raise ValueError('the run failed')
        finally:
            run_restore(boom)
    # a refusal (a string exit) of one step is no Stop: the run succeeded
    run_restore(lambda: (_ for _ in ()).throw(SystemExit('Z not homed')))


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


ONE = {'stepper': 'stepper_x'}
RAIL = {'stepper': 'stepper_x', 'motion': 'rail', 'steppers': ['stepper_x', 'stepper_x1']}


@pytest.mark.parametrize('stored, run, refusal', [
    (ONE, ONE, None),
    (RAIL, RAIL, None),
    ({}, RAIL, None),                       # pre-key dataset: nothing to compare
    # one motor's dataset on one motor's run: as before rails, whichever motor (decision 19)
    (ONE, {'stepper': 'stepper_y'}, None),
    # a dataset from before rails is one motor's FORCE_MOVE
    (ONE, RAIL, 'this one moved stepper_x alone by FORCE_MOVE, this run moves stepper_x, '
                'stepper_x1 together by G1'),
    (RAIL, ONE, 'this one moved stepper_x, stepper_x1 together by G1, this run moves '
                'stepper_x alone by FORCE_MOVE'),
    (dict(RAIL, steppers=['stepper_x', 'stepper_x1', 'stepper_x2']), RAIL, 'start a new dataset'),
])
def test_check_resume_rejects_other_drivers_or_another_motion(stored, run, refusal):
    # a rail's G1 runs every motor of it, one motor's FORCE_MOVE that one alone
    manifest = dict(stored, speeds=[58], accel=300.0, measure_time=1.25)
    if refusal is None:
        check_resume(manifest, [58], 300.0, 1.25, None, run)
        return
    with pytest.raises(SystemExit, match=refusal) as refused:
        check_resume(manifest, [58], 300.0, 1.25, None, run)
    assert failure_display('collect FAILED: %s' % refused.value.code)[:16] == 'FAIL start a new'


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
    screen.final('Belts matched: A 105 / B 105 Hz', 'Matched 105/105')
    assert 'M117 Matched 105/105' in kl.sent             # a 16-character LCD row
    assert 'M118 Belts matched: A 105 / B 105 Hz' in kl.sent


@pytest.mark.parametrize('validate, pops', [(0, True), (3, False)])
def test_the_grid_verdict_pops_up_only_when_no_validation_follows(monkeypatch, validate, pops):
    # the validation moves the motors again: a popup there would land mid-run
    from types import SimpleNamespace

    import chopper_autotune.collect as collect
    monkeypatch.setattr(collect, 'measure_combo', lambda *args: (2, 0, [100.0, 101.0], 0))
    shown = []
    screen = SimpleNamespace(update=lambda text, force=False, short=None: shown.append(('update', force)),
                             final=lambda text, short=None: shown.append(('final', True)))
    plan = [(collect.tmc.Chopper(2, 3, 5, 0), 58)]
    collect.run_grid(None, SimpleNamespace(motor='A'), None, SimpleNamespace(iterations=1, validate=validate), plan,
                     70.0, 1000.0, set(), None, screen)
    assert shown[-1] == (('final', True) if pops else ('update', True))


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


def test_a_csv_capture_finds_the_files_kalico_writes_to_klippy_tmpdir(tmp_path, monkeypatch):
    # the tool inherits klippy's TMPDIR, where Kalico's ACCELEROMETER_MEASURE writes
    import tempfile

    from chopper_autotune.collect import drop_stale_csv, wait_for_csv
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    csv = tmp_path / 'adxl345-hotend-v060.csv'
    csv.write_text('#time,x,y,z\n' + ''.join('%.4f,0,0,0\n' % (t / 100) for t in range(101)))
    assert wait_for_csv('v060', min_span_sec=1.0, timeout=5.0).resolve() == csv.resolve()
    drop_stale_csv('v060')
    assert not csv.exists()


def test_a_csv_capture_takes_the_file_the_console_named(tmp_path, monkeypatch):
    # started over SSH, the tool does not share klippy's TMPDIR, where Kalico wrote it; a
    # '[' in a path is a glob character
    import tempfile

    from chopper_autotune.collect import wait_for_csv
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path / 'elsewhere'))
    klippy_tmp = tmp_path / 'klippy[tmp]'
    klippy_tmp.mkdir()
    csv = klippy_tmp / 'adxl345-hotend-v060.csv'
    csv.write_text('#time,x,y,z\n' + ''.join('%.4f,0,0,0\n' % (t / 100) for t in range(101)))
    assert wait_for_csv('v060', min_span_sec=1.0, timeout=5.0, written=[str(csv)]) == csv
    with pytest.raises(TimeoutError, match=r'did not appear/flush: .*elsewhere/\*-v061\.csv'):
        wait_for_csv('v061', timeout=0.2)


def test_a_lost_console_fence_neither_restarts_the_chip_nor_loses_the_capture(tmp_path, monkeypatch):
    # the script ran to its end, the chip stopped and wrote the file: one more
    # ACCELEROMETER_MEASURE would start it again
    from types import SimpleNamespace

    import chopper_autotune.collect as collect
    from chopper_autotune.klippy import ConsoleFenceLost
    sent = []

    def lost(script):
        sent.append(script)
        raise ConsoleFenceLost('console fence BEGIN not seen (300 console lines captured)')
    hw = SimpleNamespace(kl=SimpleNamespace(gcode_output=lost, gcode=sent.append),
                         measure_chip='hotend')
    csv = tmp_path / 'adxl345-hotend-v060.csv'
    csv.write_text('#time,x,y,z\n' + ''.join('%.4f,0,0,0\n' % (t / 100) for t in range(50)))
    told, cutoffs = [], []
    monkeypatch.setattr(collect, 'drop_stale_csv', lambda name: None)
    monkeypatch.setattr(collect, 'wait_for_csv', lambda name, span, written, newer_than:
                        told.append(written) or cutoffs.append(newer_than) or str(csv))
    before = time.time()
    collect.capture_csv(hw, 'v060', 'G4 P100')
    assert len(sent) == 1 and told == [[]]
    # a file from before this capture is not taken for it
    assert before - 2 < cutoffs[0] <= time.time()


def test_a_file_older_than_the_capture_is_not_the_capture(tmp_path):
    # over SSH the tool cannot clean klippy's TMPDIR: last run's file under the same name
    from chopper_autotune.collect import await_flushed
    csv = tmp_path / 'raw_data_beltA.csv'
    csv.write_text('#time,x,y,z\n' + ''.join('%.4f,0,0,0\n' % (t / 100) for t in range(101)))
    os.utime(csv, (time.time() - 600, time.time() - 600))
    with pytest.raises(TimeoutError):
        await_flushed(str(csv), timeout=0.5, poll=0.05, newer_than=time.time() - 1)
    assert await_flushed(str(csv), timeout=2.0, poll=0.05) == str(csv)


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
    hw = SimpleNamespace(driver=tmc.DRIVERS['2209'], stepper='stepper_x', autotune=None, motor='A',
                         rail=[SimpleNamespace(stepper='stepper_x')])
    args = SimpleNamespace(trim=0.1, audible_weight=0.25)
    screen = SimpleNamespace(final=lambda text, short=None: finals.append(text))
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
    # settings has the section names lower-cased: CHIP= takes the name as written
    assert resolve_accel_chip({'adxl345 hotend': {}}, 'x',
                              lambda: ['printer', 'adxl345 Hotend']) == 'adxl345 Hotend'


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
    # settings has the section names lower-cased, the chip keeps its name as written
    ('beacon sensor Tool', {'beacon sensor tool': {'accel_name': 'toolaccel'}}, 'toolaccel'),
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

        def gcode_output(self, script):
            self.scripts.append(script)
            return ['// accelerometer measurements started',
                    '// Writing raw accelerometer data to %s file' % csv]

    csv = tmp_path / 'chip-v060.csv'
    csv.write_text('#time,x,y,z\n' + ''.join('%.4f,0,0,0\n' % (t / 100) for t in range(50)))
    monkeypatch.setattr(collect, 'drop_stale_csv', lambda name: None)
    told = []
    monkeypatch.setattr(collect, 'wait_for_csv', lambda name, span, written, newer_than:
                        told.append(written) or str(csv))
    kl = FakeKl()
    collect.capture_csv(collect.detect_hardware(kl, 'x'), 'v060', 'G4 P100')
    assert 'ACCELEROMETER_MEASURE CHIP=%s NAME=v060\n' % command_chip in kl.scripts[0]
    assert told == [[str(csv)]]                     # the file the console named


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
    hw = SimpleNamespace(driver=tmc.DRIVERS['2240'], stepper='stepper_x', baseline={}, autotune=None,
                         motor='A', rail=[SimpleNamespace(stepper='stepper_x')])
    args = SimpleNamespace(trim=0.1, audible_weight=0.25, tpfd=None)
    report_winner(hw, ds, args, SimpleNamespace(final=lambda text, short=None: None), top=5)
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


def test_park_moves_to_the_center_in_absolute_coordinates():
    # a macro may leave G91 behind: G0 would then move BY the center
    from types import SimpleNamespace

    from chopper_autotune.collect import park
    scripts = []
    kl = SimpleNamespace(gcode=scripts.append, settings=lambda: {})
    park(kl, SimpleNamespace(center=(130.0, 130.0)), release=False)
    lines = scripts[-1].split('\n')
    assert lines.index('G90') < lines.index('G0 X130.0 Y130.0 F6000')


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
    assert collect_mod.process_start(None, str(tmp_path)) is None   # no process_id


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
    (1000, None, False),    # the process is unknown
])
def test_klipper_extra_trusts_only_code_older_than_the_process(tmp_path, monkeypatch,
                                                               mtime, process_id, trusted):
    import os
    from types import SimpleNamespace

    import chopper_autotune.collect as collect_mod
    monkeypatch.setattr(collect_mod, '_KLIPPER_EXTRAS', {})
    monkeypatch.setattr(collect_mod, 'process_start', lambda pid: {7: 2000.0}.get(pid))
    (tmp_path / 'klippy' / 'extras').mkdir(parents=True)
    path = tmp_path / 'klippy' / 'extras' / 'force_move.py'
    path.write_text('CLEAR_HOMED')
    os.utime(path, (mtime, mtime))
    kl = SimpleNamespace(info=lambda: {'klipper_path': str(tmp_path), 'process_id': process_id})
    assert collect_mod.klipper_extra(kl, 'force_move.py') == ('CLEAR_HOMED' if trusted else '')
    # any_age reads the file whatever its age: 'updated, restart' against 'too old'
    assert collect_mod.klipper_extra(kl, 'force_move.py', any_age=True) == 'CLEAR_HOMED'
    assert collect_mod.klipper_extra(kl, 'missing.py', any_age=True) == ''


def klipper_at(tmp_path, monkeypatch, force_move, mtime=1000):
    """A running Klipper (started at 2000) whose force_move.py reads `force_move`."""
    import os
    from types import SimpleNamespace

    import chopper_autotune.collect as collect_mod
    monkeypatch.setattr(collect_mod, '_KLIPPER_EXTRAS', {})
    monkeypatch.setattr(collect_mod, 'process_start', lambda pid: 2000.0)
    if force_move is not None:
        (tmp_path / 'klippy' / 'extras').mkdir(parents=True)
        path = tmp_path / 'klippy' / 'extras' / 'force_move.py'
        path.write_text(force_move)
        os.utime(path, (mtime, mtime))
    return SimpleNamespace(info=lambda: {'klipper_path': str(tmp_path), 'process_id': 7},
                           settings=lambda: pytest.fail('asked the config of an unsupported Klipper'))


@pytest.mark.version_gate
@pytest.mark.parametrize('force_move, mtime, refusal', [
    ("clear_homed = gcmd.get('CLEAR_HOMED', '')", 1000, None),      # Klipper v0.13+, Kalico
    ("clear_homed = gcmd.get('CLEAR_HOMED', '')", 3000, 'restart the klipper service'),
    ('toolhead.set_position(pos, homing_axes=(0, 1, 2))', 1000, 'needs Klipper v0.13 or later'),
    (None, 1000, 'cannot check the Klipper version'),
])
def test_only_the_current_klipper_is_supported(tmp_path, monkeypatch, force_move, mtime, refusal):
    # before v0.13 (Kalico v2026.08.00) SET_KINEMATIC_POSITION marks every axis homed,
    # and before December 2023 FORCE_MOVE is measured on the standstill after the move
    from chopper_autotune.collect import UnsupportedKlipper, detect_hardware, require_current_klipper
    kl = klipper_at(tmp_path, monkeypatch, force_move, mtime)
    if refusal is None:
        require_current_klipper(kl)
        return
    # a stop of the whole run: CHOPPER_DEMO REPORT=1 would read a plain one as 'motor skipped'
    with pytest.raises(UnsupportedKlipper, match=refusal):
        detect_hardware(kl, 'x')                    # every tool that moves starts there



def test_a_display_text_is_what_an_lcd_can_draw():
    # an LCD draws bytes: a character beyond ASCII came out as 'ΓÇö', and '~' is glyph markup
    from chopper_autotune.collect import display_text
    assert display_text('Belt A \u2014 moves \u00b7 B \u2192 C\u2026 ~5 \u00b0') == \
        'Belt A - moves | B > C... -5 '


def test_a_row_takes_the_items_that_fit_first_first():
    from chopper_autotune.collect import fit_row
    assert fit_row('A pk', ['60', '120', '180', '240']) == 'A pk 60,120,180+'      # '+': more
    assert fit_row('A pk', ['60', '120']) == 'A pk 60,120'
    assert fit_row('A pk', ['-']) == 'A pk -'


def test_every_display_text_has_its_lcd_form():
    # a run path no test drives still reaches the display: every Screen call names the
    # form a 16-character LCD row shows, unless its text is a short literal itself
    import ast
    import glob

    from chopper_autotune.collect import LCD_WIDTH
    missing = []
    for path in sorted(glob.glob(os.path.join(os.path.dirname(__file__), '..', 'chopper_autotune', '*.py'))):
        with open(path) as source:
            tree = ast.parse(source.read())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr in ('update', 'final') \
                    and 'screen' in ast.unparse(func.value).lower():
                positional_short = func.attr == 'final' and len(node.args) >= 2
            elif isinstance(func, ast.Name) and func.id == 'cue':
                positional_short = len(node.args) >= 2
            else:
                continue
            text = node.args[0] if node.args else None
            literal = isinstance(text, ast.Constant) and len(text.value) <= LCD_WIDTH
            if not (positional_short or literal or any(k.arg == 'short' for k in node.keywords)):
                missing.append('%s:%d %s' % (os.path.basename(path), node.lineno, ast.unparse(node)[:60]))
    assert missing == []
