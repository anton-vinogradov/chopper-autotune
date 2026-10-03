"""The tools against a printer built from the release's own modules (tests/klipper_front.py):
every command they send must reach the real handler and do what the tool means, with no
error line in the console. A fake that repeats our own assumptions passed a command
Klipper then refused, or took in a way the tool never meant."""
import json
import os
import types

import pytest

import fake_klipper
import klipper_front
from chopper_autotune import collect, tmc
from chopper_autotune.cli import build_parser

SOURCES = klipper_front.Front.sources()


def require(source):
    if source is None:
        message = 'no Klipper sources in %s: run tests/fetch_klipper_sources.sh' % klipper_front.SRC
        if os.environ.get('CHOPPER_CONTRACT'):
            pytest.fail(message)
        pytest.skip(message)


def printer_cfg(kinematics: str = 'corexy', driver: str = '2209', extra: str = '') -> str:
    """printer.cfg of a printer with the tool installed: X/Y on `driver`, Z, an ADXL345."""
    rails = ''.join('[stepper_%s]\nstep_pin: P%d\ndir_pin: P%d\nenable_pin: !P%d\nmicrosteps: 16\n'
                    'rotation_distance: %s\nendstop_pin: ^E%s\nposition_endstop: 0\n'
                    'position_max: %s\n\n' % (axis, index, index + 10, index + 20,
                                              8 if axis == 'z' else 40, axis, 200 if axis == 'z' else 250)
                    for index, axis in enumerate('xyz'))
    # Kalico wants sense_resistor written out, and a TMC2240's rref
    drivers = ''.join('[tmc%s stepper_%s]\n%s: P%d\nrun_current: 0.8\n%s\n\n'
                      % (driver, axis, 'cs_pin' if driver in ('2130', '2660', '5160') else 'uart_pin',
                         index + 30, 'rref: 12000' if driver == '2240' else 'sense_resistor: 0.110')
                      for index, axis in enumerate('xy'))
    return ('[printer]\nkinematics: %s\nmax_velocity: 500\nmax_accel: 10000\n\n%s%s'
            '[adxl345]\ncs_pin: P40\n\n[resonance_tester]\naccel_chip: adxl345\n'
            'probe_points: 125, 125, 20\n\n[force_move]\nenable_force_move: True\n\n[respond]\n\n%s'
            % (kinematics, rails, drivers, extra))


def assert_clean(front):
    """No command refused, none unknown, no shutdown: what the console shows the user. And
    the fake Klipper of the other tests takes every line the real parser took."""
    errors = [line for line in front.console if line.startswith('!!') or 'Unknown command' in line]
    assert not front.crashes, front.crashes
    assert not errors and not front.printer.shutdowns, errors or front.printer.shutdowns
    assert not [line for script in front.scripts for line in script.split('\n')
                if fake_klipper.malformed(line)]


@pytest.fixture(autouse=True)
def no_poll_wait(monkeypatch):
    monkeypatch.setattr(collect, 'PREFLIGHT_SEC', 0)


COLLECT_CASES = [(kinematics, driver, stealth)
                 for driver, switch in sorted((name, d.spreadcycle_switch) for name, d in tmc.DRIVERS.items())
                 for stealth in ((False, True) if switch else (False,))
                 for kinematics in (('corexy', 'cartesian') if driver == '2209' else ('corexy',))]


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('kinematics, driver, stealth', COLLECT_CASES)
def test_a_grid_collect_moves_the_motor_on_each_combo_and_puts_the_driver_back(
        source, kinematics, driver, stealth, tmp_path):
    require(source)
    section = 'tmc%s stepper_x' % driver
    cfg = printer_cfg(kinematics, driver)
    if stealth:
        cfg = cfg.replace('[%s]\n' % section, '[%s]\nstealthchop_threshold: 999999\n' % section)
    front = klipper_front.Front(source, cfg)
    chip = front.chips[section]
    stock = chip.chopper()
    status, _ = run_tool(front, collect.collect, [
        'collect', '--axis', 'x', '--speed', '60', '--tbl', '1:1', '--toff', '3:4', '--hstrt', '4:4',
        '--hend', '3:3', '--iterations', '1', '--yes', '--no-raw', '--dataset', str(tmp_path / 'grid')])
    assert status == 0
    assert_clean(front)
    moves = front.moves
    assert {move['stepper'] for move in moves} == {'stepper_x'}
    # each combo on the chip while its moves ran, forward and back, in spreadCycle
    ran = {(move['chips'][section]['toff'], move['distance'] > 0) for move in moves}
    assert ran == {(3, False), (3, True), (4, False), (4, True)}
    field, spread, _ = tmc.DRIVERS[driver].spreadcycle_switch or ('chm', 0, 0)
    assert all(move['chips'][section][name] == value for move in moves
               for name, value in (('tbl', 1), ('hstrt', 4), ('hend', 3), (field, spread)))
    assert chip.chopper() == stock                  # the config's registers and mode back
    assert front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes'] == 'xy'


def run_tool(front, tool, argv):
    kl = front.connect()
    try:
        return tool(kl, build_parser().parse_args(argv))
    finally:
        kl.close()


def limits_in_force(front) -> dict:
    status = front.status({'toolhead': ['max_accel', 'max_velocity', 'minimum_cruise_ratio'],
                           'gcode_move': ['speed_factor', 'absolute_coordinates']})
    return dict(status['toolhead'], **status['gcode_move'])


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('kinematics', ['corexy', 'cartesian'])
def test_current_bisects_on_the_real_current_helper_and_puts_the_exact_current_back(
        source, kinematics):
    # 0.566 A sent as 0.57 set IRUN 18 instead of 17 until Klipper restarted
    from chopper_autotune.current import current_tune
    require(source)
    front = klipper_front.Front(source, printer_cfg(kinematics).replace('run_current: 0.8',
                                                                       'run_current: 0.566'))
    chip = front.chips['tmc2209 stepper_x']
    configured, limits = (chip.field('irun'), chip.field('vsense')), limits_in_force(front)
    assert run_tool(front, current_tune, ['current', '--motor', 'a', '--yes']) == 0
    assert_clean(front)
    irun = {value for register, value in chip.writes if register == 'IHOLD_IRUN'}
    assert len(irun) > 1                                   # the rungs reached the driver
    assert (chip.field('irun'), chip.field('vsense')) == configured
    assert limits_in_force(front) == limits
    assert front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes'] == 'xy'


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('kinematics', ['corexy', 'cartesian'])
def test_the_envelope_runs_its_ladders_and_puts_the_printer_back(source, kinematics):
    import chopper_autotune.envelope as env
    require(source)
    front = klipper_front.Front(source, printer_cfg(kinematics).replace(
        '[tmc2209 stepper_x]\n', '[tmc2209 stepper_x]\nstealthchop_threshold: 999999\n'))
    chips = {section: chip.chopper() for section, chip in front.chips.items()}
    limits = limits_in_force(front)
    assert run_tool(front, env.envelope, ['envelope', '--yes']) == 0
    assert_clean(front)
    chip = front.chips['tmc2209 stepper_x']             # stealthChop off for the strokes only
    assert [chip.fields.get_field('en_spreadcycle', value, register)
            for register, value in chip.writes if register == 'GCONF'][-2:] == [1, 0]
    assert {section: chip.chopper() for section, chip in front.chips.items()} == chips
    assert limits_in_force(front) == limits
    assert front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes'] == 'xy'


EXTRA_STEPPERS = ''.join('[%s]\nstep_pin: P%d\ndir_pin: P%d\nenable_pin: !P%d\nmicrosteps: 16\n'
                         'rotation_distance: 22\n\n' % (section, 60 + i, 70 + i, 80 + i)
                         for i, section in enumerate(['extruder', 'extruder_stepper belted',
                                                      'manual_stepper cutter']))


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('cycle', [False, True])
def test_releasing_the_gantry_leaves_z_and_the_other_motors_holding(source, cycle):
    # hands move the head next: X/Y and the extruders off and unhomed, Z holds and stays
    # homed, a cutter is none of the tool's business
    require(source)
    front = klipper_front.Front(source, printer_cfg(extra=EXTRA_STEPPERS))
    front.run('G28\nSET_STEPPER_ENABLE STEPPER="manual_stepper cutter" ENABLE=1\n'
              'SET_STEPPER_ENABLE STEPPER="extruder_stepper belted" ENABLE=1\n'
              'SET_STEPPER_ENABLE STEPPER=extruder ENABLE=1')
    run_tool(front, lambda kl, args: collect.release_gantry(kl, cycle), ['status'])
    assert_clean(front)
    assert front.status({'stepper_enable': None})['stepper_enable']['steppers'] == {
        'stepper_x': False, 'stepper_y': False, 'stepper_z': True, 'extruder': False,
        'extruder_stepper belted': False, 'manual_stepper cutter': True}
    assert front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes'] == 'z'


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('driver, register, value', [
    ('2209', 'DRV_STATUS', 1 << 0),                 # otpw: the over-temperature warning
    ('2240', 'ADC_TEMP', 2038 + int(110 * 7.7)),    # a TMC2240 reads its die: 110 C
])
def test_a_hot_driver_stops_the_run_before_any_move_and_the_gantry_goes_off(
        source, driver, register, value, tmp_path):
    # #133: a TMC2240 warned twice, then shut itself and Klipper down
    require(source)
    front = klipper_front.Front(source, printer_cfg(driver=driver))
    front.run('G28')
    chip = front.chips['tmc%s stepper_x' % driver]
    stock = chip.chopper()
    chip.reads[register] = value
    with pytest.raises(collect.DriverTooHot):
        run_tool(front, collect.collect, [
            'collect', '--axis', 'x', '--speed', '60', '--toff', '3:4', '--yes', '--no-raw',
            '--dataset', str(tmp_path / 'grid')])
    assert_clean(front)
    assert front.moves == [] and chip.chopper() == stock
    steppers = front.status({'stepper_enable': None})['stepper_enable']['steppers']
    assert (steppers['stepper_x'], steppers['stepper_y'], steppers['stepper_z']) == (False, False, True)


TUNED = ('[tmc2209 stepper_x]\n', '[tmc2209 stepper_x]\ndriver_TBL: 1\ndriver_TOFF: 4\n'
         'driver_HSTRT: 2\ndriver_HEND: 3\n')


@pytest.mark.parametrize('source', SOURCES)
def test_a_speed_scan_finds_the_resonance_with_the_stock_chopper(source, tmp_path):
    # a tuned chopper masks the very peaks the scan looks for
    from chopper_autotune.find_speed import scan
    require(source)
    front = klipper_front.Front(source, printer_cfg().replace(*TUNED))
    chip = front.chips['tmc2209 stepper_x']
    tuned = chip.chopper()
    status, speed = run_tool(front, scan, [
        'find-speed', '--axis', 'x', '--min-speed', '30', '--max-speed', '90', '--step', '10',
        '--yes', '--no-raw', '--dataset', str(tmp_path / 'scan')])
    assert (status, speed) == (0, 60)
    assert_clean(front)
    stock = tmc.DRIVERS['2209'].default.fields()
    assert all({name: move['chips']['tmc2209 stepper_x'][name] for name in stock} == stock
               for move in front.moves)
    assert chip.chopper() == tuned


@pytest.mark.parametrize('source', SOURCES)
def test_the_map_runs_on_the_registers_in_use(source, tmp_path):
    from chopper_autotune.resonance_map import resonance_map
    require(source)
    front = klipper_front.Front(source, printer_cfg().replace(*TUNED))
    chip = front.chips['tmc2209 stepper_x']
    tuned = chip.chopper()
    assert run_tool(front, resonance_map, [
        'map', '--axis', 'x', '--min-speed', '40', '--max-speed', '80', '--step', '10',
        '--print-speed', '60', '--yes', '--no-raw', '--dataset', str(tmp_path / 'map')]) == 0
    assert_clean(front)
    assert {move['speed'] for move in front.moves} == {40, 50, 60, 70, 80}
    assert all(move['chips']['tmc2209 stepper_x'] == tuned for move in front.moves)
    assert chip.chopper() == tuned


@pytest.mark.parametrize('source', SOURCES)
def test_the_demo_alternates_the_stock_and_the_tuned_chopper_and_ends_on_the_tuned(source):
    from chopper_autotune import demo
    require(source)
    front = klipper_front.Front(source, printer_cfg().replace(*TUNED))
    chip = front.chips['tmc2209 stepper_x']
    tuned = chip.chopper()
    assert run_tool(front, demo.demo, ['demo', '--axis', 'x', '--speed', '60', '--report',
                                       '--iterations', '1']) == 0
    assert_clean(front)
    played = {tuple(sorted(move['chips']['tmc2209 stepper_x'].items())) for move in front.moves}
    stock = dict(tuned, **tmc.DRIVERS['2209'].default.fields())
    assert played == {tuple(sorted(tuned.items())), tuple(sorted(stock.items()))}
    assert chip.chopper() == tuned


@pytest.mark.parametrize('source', SOURCES)
def test_a_whole_tune_finds_the_quietest_chopper_of_each_motor(source, monkeypatch):
    # the printer's model: a resonance at 60 mm/s, toff 4 and hend 3 the quietest
    from chopper_autotune import tune
    require(source)
    front = klipper_front.Front(source, printer_cfg())
    stock = {section: chip.chopper() for section, chip in front.chips.items()}
    kl = front.connect()
    monkeypatch.setattr(tune, 'Klippy', lambda path: types.SimpleNamespace(connect=lambda: kl))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<front>')
    assert tune.run_tune(build_parser().parse_args(['tune', '--no-raw'])) == 0
    assert_clean(front)
    winners = {}
    for root in sorted((collect.RESULTS_HOME / 'datasets').iterdir()):
        manifest = json.loads((root / 'manifest.json').read_text())
        if manifest.get('winner'):
            winners[manifest['stepper']] = (manifest['winner']['toff'], manifest['winner']['hend'])
    assert winners == {'stepper_x': (4, 3), 'stepper_y': (4, 3)}
    assert {section: chip.chopper() for section, chip in front.chips.items()} == stock


HOTEND = ('[extruder]\nstep_pin: P60\ndir_pin: P61\nenable_pin: !P62\nmicrosteps: 16\n'
          'rotation_distance: 22\nheater_pin: P63\nsensor_type: thermistor\nsensor_pin: P64\n'
          'min_temp: 0\nmax_temp: 300\ncontrol: pid\npid_Kp: 22\npid_Ki: 1\npid_Kd: 100\n'
          'nozzle_diameter: 0.4\nfilament_diameter: 1.75\n\n'
          '[tmc2209 extruder]\nuart_pin: P65\nrun_current: 0.5\nsense_resistor: 0.110\n\n')


@pytest.mark.parametrize('source', SOURCES)
def test_the_extruder_tune_heats_its_own_hotend_and_leaves_it_off(source):
    # M104 heats the ACTIVE extruder: on an IDEX the other hotend
    from chopper_autotune.extruder import extruder_tune
    require(source)
    front = klipper_front.Front(source, printer_cfg(extra=HOTEND))
    chip, heater = front.chips['tmc2209 extruder'], front.printer.objects['extruder']
    stock = chip.chopper()
    temperatures = []
    real_heat = front.heat
    front.heat = lambda eventtime: (real_heat(eventtime),
                                    temperatures.append(heater.get_temp(eventtime)))
    assert run_tool(front, extruder_tune, ['extruder', '--temp', '210', '--speed', '5',
                                           '--yes']) == 0
    assert_clean(front)
    assert (210., 210.) in temperatures and {move['stepper'] for move in front.moves} == {'extruder'}
    assert heater.target_temp == 0 and chip.chopper() == stock
    assert front.status({'stepper_enable': None})['stepper_enable']['steppers']['extruder'] is False


@pytest.mark.parametrize('source', SOURCES)
def test_without_respond_the_run_talks_to_the_display_and_the_log_only(source, tmp_path):
    # RESPOND is [respond]'s: without it every update was an 'Unknown command' line
    require(source)
    front = klipper_front.Front(source, printer_cfg().replace('[respond]\n', ''))
    status, _ = run_tool(front, collect.collect, [
        'collect', '--axis', 'x', '--speed', '60', '--toff', '3:4', '--hstrt', '4:4',
        '--hend', '3:3', '--iterations', '1', '--yes', '--no-raw', '--dataset', str(tmp_path / 'g')])
    assert status == 0
    assert_clean(front)
    assert not [script for script in front.scripts if 'RESPOND' in script or 'M118' in script]
    assert front.printer.objects['display_status'].message
