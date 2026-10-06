"""The tools against a printer built from the release's own modules (tests/klipper_front.py):
every command they send must reach the real handler and do what the tool means, with no
error line in the console. A fake that repeats our own assumptions passed a command
Klipper then refused, or took in a way the tool never meant."""
import itertools
import json
import math
import os
import types

import pytest

import fake_klipper
import klipper_front
from chopper_autotune import collect, current, tmc
from chopper_autotune.cli import build_parser
from chopper_autotune.metrics import vibration_score

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
            'probe_points: 125, 125, 20\n\n[force_move]\nenable_force_move: True\n\n[respond]\n\n'
            '[display_status]\n\n%s' % (kinematics, rails, drivers, extra))


AWD = {
    # meijjaa's cross gantry (#129): sensorless TMC5160s, StallGuard on the main motor alone
    'meijjaa': {'kinematics': 'cartesian', 'driver': '5160', 'sensorless': True, 'size': (300, 210),
                'endstop': 'min', 'limits': 'max_accel: 500\nminimum_cruise_ratio: 0.5'},
    # a Voron 2.4 350 with four X/Y motors, homing to the max: on switches, or sensorless
    'voron-2209': {'kinematics': 'corexy', 'driver': '2209', 'sensorless': False, 'size': (350, 350),
                   'endstop': 'max', 'limits': 'max_accel: 3000'},
    'voron-5160': {'kinematics': 'corexy', 'driver': '5160', 'sensorless': True, 'size': (350, 350),
                   'endstop': 'max', 'limits': 'max_accel: 3000'},
}


def awd_cfg(printer: str, twins: bool = True) -> str:
    """printer.cfg of an AWD printer with the tool installed: X on stepper_x and
    stepper_x1, Y on stepper_y and stepper_y1, each motor with a driver and an enable pin
    of its own; a Z, an ADXL345. twins=False: the same printer with a motor per axis."""
    spec = AWD[printer]
    driver = spec['driver']
    pins = itertools.count(100)

    def pin(prefix: str = '') -> str:
        return '%sP%d' % (prefix, next(pins))
    rails = drivers = ''
    for axis, size in zip('xy', spec['size']):
        for name in ['stepper_' + axis] + ['stepper_%s1' % axis] * twins:
            rails += ('[%s]\nstep_pin: %s\ndir_pin: %s\nenable_pin: %s\nmicrosteps: 16\n'
                      'rotation_distance: 40\n' % (name, pin(), pin(), pin('!')))
            drivers += '[tmc%s %s]\n%s: %s\nrun_current: 0.8\nsense_resistor: %s\n' % (
                driver, name, 'cs_pin' if driver == '5160' else 'uart_pin', pin(),
                '0.075' if driver == '5160' else '0.110')
            if name == 'stepper_' + axis:           # a twin homes on its main motor's endstop
                if spec['sensorless']:
                    rails += 'endstop_pin: tmc%s_%s:virtual_endstop\nhoming_retract_dist: 0\n' % (
                        driver, name)
                    drivers += 'diag1_pin: %s\ndriver_SGT: 1\n' % pin('^!')
                else:
                    rails += 'endstop_pin: %s\n' % pin('^')
                rails += 'position_endstop: %d\nposition_max: %d\n' % (
                    0 if spec['endstop'] == 'min' else size, size)
            rails += '\n'
            drivers += '\n'
    rails += ('[stepper_z]\nstep_pin: %s\ndir_pin: %s\nenable_pin: %s\nmicrosteps: 16\n'
              'rotation_distance: 8\nendstop_pin: %s\nposition_endstop: 0\nposition_max: 200\n\n'
              % (pin(), pin(), pin('!'), pin('^')))
    return ('[printer]\nkinematics: %s\nmax_velocity: 500\n%s\n\n%s%s[adxl345]\ncs_pin: %s\n\n'
            '[resonance_tester]\naccel_chip: adxl345\nprobe_points: %d, %d, 20\n\n'
            '[force_move]\nenable_force_move: True\n\n[respond]\n\n[display_status]\n'
            % (spec['kinematics'], spec['limits'], rails, drivers, pin(),
               spec['size'][0] / 2, spec['size'][1] / 2))


def assert_clean(front):
    """No command refused, none unknown, no shutdown: what the console shows the user. And
    the fake Klipper of the other tests takes every line the real parser took."""
    errors = [line for line in front.console if line.startswith('!!') or 'Unknown command' in line]
    assert not front.crashes, front.crashes
    assert not errors and not front.printer.shutdowns, errors or front.printer.shutdowns
    assert not [line for script in front.scripts for line in script.split('\n')
                if fake_klipper.malformed(line)]


@pytest.fixture(autouse=True)
def printer_time(monkeypatch):
    """The tools' sleeps let the printer's time run on: Klipper polls the drivers meanwhile."""
    monkeypatch.setattr(collect, 'time', klipper_front.Clock())


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
    # each move as planned, net zero: a FORCE_MOVE skips the range check
    manifest = json.loads((tmp_path / 'grid' / 'manifest.json').read_text())
    assert {(abs(round(move['distance'], 3)), move['speed'], move['accel']) for move in moves} \
        == {(manifest['travel_distance'], 60, manifest['accel'])}
    assert sum(move['distance'] for move in moves) == pytest.approx(0, abs=1e-6)
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


AT_RUNTIME = 'M204 S5000\nM220 S50\nSET_VELOCITY_LIMIT MINIMUM_CRUISE_RATIO=0.3'


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
    front.run(AT_RUNTIME)                       # what a print left: it comes back after
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
    front.run(AT_RUNTIME)
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
    if source.startswith('kalico'):
        pytest.skip('Kalico loads respond with or without [respond]')
    front = klipper_front.Front(source, printer_cfg().replace('[respond]\n', ''))
    status, _ = run_tool(front, collect.collect, [
        'collect', '--axis', 'x', '--speed', '60', '--toff', '3:4', '--hstrt', '4:4',
        '--hend', '3:3', '--iterations', '1', '--yes', '--no-raw', '--dataset', str(tmp_path / 'g')])
    assert status == 0
    assert_clean(front)
    assert not [script for script in front.scripts if 'RESPOND' in script or 'M118' in script]
    assert front.printer.objects['display_status'].message


@pytest.mark.parametrize('source', SOURCES)
def test_a_driver_hot_from_before_the_restart_stops_the_run_after_klipper_polled_it(source):
    # Klipper reads a TMC2240's die only once a second after the motor is enabled: the
    # check right after enabling would see no temperature at all
    require(source)
    front = klipper_front.Front(source, printer_cfg(driver='2240'))
    front.chips['tmc2240 stepper_x'].reads['ADC_TEMP'] = 2038 + int(110 * 7.7)
    with pytest.raises(collect.DriverTooHot):
        run_tool(front, collect.collect, ['collect', '--axis', 'x', '--speed', '60', '--toff', '3:4',
                                          '--yes', '--no-raw'])
    assert_clean(front)
    assert not [script for script in front.scripts if 'G28' in script or 'SET_TMC_FIELD' in script]


@pytest.mark.parametrize('source', SOURCES)
def test_without_an_enable_pin_each_combo_still_reaches_the_moving_motor(source, tmp_path):
    # enabling a motor without its own enable pin brings toff back from the config: a
    # register written before that would be undone (wake_stepper)
    require(source)
    front = klipper_front.Front(source, printer_cfg('cartesian').replace('enable_pin: !P20\n', ''))
    status, _ = run_tool(front, collect.collect, [
        'collect', '--axis', 'x', '--speed', '60', '--tbl', '1:1', '--toff', '5:6', '--hstrt', '4:4',
        '--hend', '3:3', '--iterations', '1', '--yes', '--no-raw', '--dataset', str(tmp_path / 'g')])
    assert status == 0
    assert_clean(front)
    assert {(move['chips']['tmc2209 stepper_x']['toff'], move['distance'] > 0)
            for move in front.moves} == {(5, False), (5, True), (6, False), (6, True)}


@pytest.mark.parametrize('source', SOURCES)
def test_a_driver_error_mid_run_stops_it_at_the_next_command(source, tmp_path):
    # #133: a shutdown answers every command with the same error; the run retried 95 moves
    require(source)
    front = klipper_front.Front(source, printer_cfg())
    chip = front.chips['tmc2209 stepper_x']
    force_move = front.printer.objects['force_move']
    real_move = force_move.manual_move

    def manual_move(*args):
        real_move(*args)
        chip.reads['DRV_STATUS'] = chip.fields.all_fields['DRV_STATUS']['s2ga']   # a short
    force_move.manual_move = manual_move
    with pytest.raises(collect.KlipperShutdown):
        run_tool(front, collect.collect, ['collect', '--axis', 'x', '--speed', '60', '--toff', '3:4',
                                          '--yes', '--no-raw', '--dataset', str(tmp_path / 'g')])
    assert len(front.moves) == 1 and len(front.printer.shutdowns) == 1


@pytest.mark.parametrize('source', SOURCES)
def test_a_stop_mid_rung_puts_the_exact_current_back(source):
    # a rung lowers the current: a run stopped there must not leave the motor on it
    from chopper_autotune.current import current_tune
    require(source)
    front = klipper_front.Front(source, printer_cfg().replace('run_current: 0.8',
                                                             'run_current: 0.566'))
    chip = front.chips['tmc2209 stepper_x']
    configured = chip.field('irun')
    real_write = chip.set_register

    def set_register(reg_name, value, print_time=None):
        real_write(reg_name, value, print_time)
        if reg_name == 'IHOLD_IRUN' and chip.field('irun') < configured:
            chip.reads['DRV_STATUS'] = 1                 # otpw on the lowered current
    chip.set_register = set_register
    with pytest.raises(collect.DriverTooHot):
        run_tool(front, current_tune, ['current', '--motor', 'a', '--yes'])
    assert_clean(front)
    assert min(chip.fields.get_field('irun', value, 'IHOLD_IRUN')
               for register, value in chip.writes if register == 'IHOLD_IRUN') < configured
    assert chip.field('irun') == configured


@pytest.mark.parametrize('source', SOURCES)
def test_the_show_on_both_motors_puts_back_the_printer_it_found(source):
    from chopper_autotune import demo
    require(source)
    front = klipper_front.Front(source, printer_cfg().replace(*TUNED).replace(
        '[tmc2209 stepper_y]\n', TUNED[1].replace('stepper_x', 'stepper_y')))
    front.run(AT_RUNTIME)
    chips = {section: chip.chopper() for section, chip in front.chips.items()}
    limits = limits_in_force(front)
    assert run_tool(front, demo.showcase_together, ['demo', '--speed', '60', '--rounds', '1',
                                                    '--repeats', '1']) == 0
    assert_clean(front)
    assert {section: chip.chopper() for section, chip in front.chips.items()} == chips
    assert limits_in_force(front) == limits


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', AWD)
def test_an_awd_printer_homes_and_each_twin_takes_its_own_registers(source, printer):
    require(source)
    front = klipper_front.Front(source, awd_cfg(printer))
    driver = AWD[printer]['driver']
    assert sorted(front.chips) == sorted('tmc%s stepper_%s' % (driver, name)
                                         for name in ('x', 'x1', 'y', 'y1'))
    stock = {section: chip.chopper() for section, chip in front.chips.items()}
    assert front.run('G28 X Y\nSET_TMC_FIELD STEPPER=stepper_x1 FIELD=toff VALUE=6') is None
    assert_clean(front)
    assert [section for section, chip in front.chips.items()
            if chip.chopper() != stock[section]] == ['tmc%s stepper_x1' % driver]
    assert front.status({'toolhead': ['homed_axes']})['toolhead']['homed_axes'] == 'xy'


TURNS = [('meijjaa', (1, 0), 'x'), ('meijjaa', (0, 1), 'y'), ('meijjaa', (1, 1), 'xy'),
         ('voron-2209', (1, 1), 'x'), ('voron-2209', (1, -1), 'y'), ('voron-2209', (1, 0), 'xy')]


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer, head, rails', TURNS)
def test_a_head_move_turns_the_rails_whose_belts_run_and_no_other(source, printer, head, rails):
    # every X/Y motor stepped on any CoreXY move: motor A's diagonal switched B's motors on
    require(source)
    front = klipper_front.Front(source, awd_cfg(printer))
    x, y = (size / 2 for size in AWD[printer]['size'])
    off = '\n'.join('SET_STEPPER_ENABLE STEPPER=%s ENABLE=0' % name
                    for axis in 'xy' for name in front.rail(axis))
    assert front.run('G28 X Y\nG90\nG1 X%s Y%s F6000\nM400\n%s\nG1 X%s Y%s F3000\nM400'
                     % (x, y, off, x + 20 * head[0], y + 20 * head[1])) is None
    assert_clean(front)
    steppers = front.status({'stepper_enable': None})['stepper_enable']['steppers']
    assert sorted(name for name, on in steppers.items() if on) \
        == sorted(name for axis in rails for name in front.rail(axis))
    move = front.head_moves[-1]
    assert move['start'][:2] + move['end'][:2] == pytest.approx(
        (x, y, x + 20 * head[0], y + 20 * head[1]))
    assert ''.join(axis for axis in 'xy' if move['belts'][axis] > klipper_front.MOVING) == rails
    assert front.moves == []                        # FORCE_MOVE's list holds FORCE_MOVEs alone


def rail_stroke(front, kl, axis: str, combos: dict, speed: float = 60.) -> float:
    """A measurement on G1 as a rail would run one, by hand: each driver of the rail on its
    combo, the head across the bed's center along the rail's belt at `speed` (belt mm/s),
    the window cut from the stream (steady_window); its median vibration."""
    accel, measure_time = 500., 1.
    vec = current.stress_vector(front.fileconfig.get('printer', 'kinematics'), axis)
    factor = math.hypot(*vec)                       # belt mm per head mm along vec
    half = collect.travel_for(speed, accel, measure_time) / factor ** 2 / 2
    center = [front.fileconfig.getfloat('stepper_' + name, 'position_max') / 2 for name in 'xy']
    start, end = ([at + sign * part * half for at, part in zip(center, vec)] for sign in (-1, 1))
    kl.gcode('\n'.join(tmc.set_fields_script(stepper, combo.fields())
                       for stepper, combo in combos.items()))
    kl.gcode('G1 X%.3f Y%.3f F6000\nM400\nM204 S%.3f' % (start[0], start[1], accel / factor))
    t_end, data = collect.capture_stream(types.SimpleNamespace(kl=kl), 'G1 X%.3f Y%.3f F%.3f' % (
        end[0], end[1], speed / factor * 60), measure_time + speed / accel)
    steady = collect.window(data, *collect.steady_window(t_end, speed, accel, measure_time, .1))
    return vibration_score(steady, 0.)['median_magnitude']


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('printer', ['meijjaa', 'voron-2209'])
def test_a_g1_stroke_streams_the_vibration_of_every_driver_on_its_rail(source, printer):
    # a twin left on other registers than its main motor's must be heard, as on a printer
    require(source)
    front = klipper_front.Front(source, awd_cfg(printer))
    quiet, loud = tmc.Chopper(2, 4, 4, 3), tmc.Chopper(2, 7, 4, 9)
    kl = front.connect()
    try:
        kl.subscribe_accel('adxl345')
        kl.gcode('G28 X Y\nG90')
        heard = [rail_stroke(front, kl, 'x', {'stepper_x': main, 'stepper_x1': twin})
                 for main, twin in ((quiet, quiet), (quiet, loud), (loud, loud))]
    finally:
        kl.close()
    assert_clean(front)
    assert heard[0] < heard[1] < heard[2], heard
    stroke = front.head_moves[-1]
    assert (stroke['belts']['x'], stroke['belts']['y']) == pytest.approx((60, 0), abs=1e-3)
    assert stroke['chips']['tmc%s stepper_x1' % AWD[printer]['driver']]['toff'] == loud.toff


@pytest.mark.parametrize('source', SOURCES)
def test_a_saved_winner_is_what_the_driver_holds_after_the_restart(source):
    # the driver_* lines a save writes, read by the release's own TMC module
    from chopper_autotune import analyze
    require(source)
    mk = klipper_front.FrontMoonraker(source, printer_cfg())
    stock = mk.front.chips['tmc2209 stepper_y'].chopper()
    winner = tmc.Chopper(1, 5, 4, 2)
    analyze.run_save(mk, [({'driver': '2209', 'stepper': 'stepper_x'}, winner)])
    assert mk.restarts == 1 and mk.uploads == ['printer.chopper-backup.cfg', 'printer.cfg']
    chips = mk.front.chips
    assert {name: chips['tmc2209 stepper_x'].field(name) for name in winner.fields()} \
        == winner.fields()
    assert chips['tmc2209 stepper_y'].chopper() == stock
    assert_clean(mk.front)
