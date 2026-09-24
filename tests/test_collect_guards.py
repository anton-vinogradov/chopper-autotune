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
    hw = SimpleNamespace(driver=tmc.DRIVERS['2209'], stepper='stepper_x', autotune=None)
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


def test_capture_csv_names_the_chip_by_its_section_word(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import chopper_autotune.collect as collect
    csv = tmp_path / 'hotend-v060.csv'
    csv.write_text('#time,x,y,z\n' + ''.join('%.4f,0,0,0\n' % (t / 100) for t in range(50)))
    monkeypatch.setattr(collect, 'drop_stale_csv', lambda name: None)
    monkeypatch.setattr(collect, 'wait_for_csv', lambda name, span: str(csv))
    scripts = []
    hw = SimpleNamespace(kl=SimpleNamespace(gcode=scripts.append), accel_chip='adxl345 hotend')
    collect.capture_csv(hw, 'v060', 'G4 P100')
    # ACCELEROMETER_MEASURE CHIP= takes the name word of [adxl345 hotend], not the section
    assert 'ACCELEROMETER_MEASURE CHIP=hotend NAME=v060' in scripts[0]
    assert 'CHIP=adxl345 hotend' not in scripts[0]


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
    hw = SimpleNamespace(driver=tmc.DRIVERS['2240'], stepper='stepper_x', baseline={}, autotune=None)
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
