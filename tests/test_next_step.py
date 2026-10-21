"""What a command tells the person to run next: one Klipper command, ready to paste into
the console. A command run on its own names it in the log and the console; inside
CHOPPER_TUNE only the tune's own one is said."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from chopper_autotune import collect, current, find_speed, moonraker, tmc, tune
from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
from chopper_autotune.dataset import Dataset


def console(printer) -> 'list[str]':
    """The lines the stub printer's console got: RESPOND's message, M118's text."""
    lines = []
    for script in printer.log:
        if script.startswith('RESPOND '):
            lines.append(script.split(' MSG="', 1)[1][:-1])
        elif script.startswith('M118 '):
            lines.append(script[len('M118 '):])
    return lines


def on_its_own(printer, monkeypatch, module):
    """The command's own entry point talks to the stub printer."""
    monkeypatch.setattr(module, 'Klippy', lambda sock: printer.kl())
    monkeypatch.setattr(module, 'find_socket', lambda explicit=None: '<sock>')


def scanned(monkeypatch, vibration):
    """A speed scan that measures vibration(speed, motor)."""
    def move(hw, ds, args, record, speed, cruise, travel, direction, accel, before_move):
        record.update(status='ok', score={'median_magnitude': vibration(speed, hw.motor)})
        ds.append(record)
        return record
    monkeypatch.setattr(find_speed, 'measure_move', move)


def without_peak(speed):
    """The vibration only falls with speed, the most at 20 mm/s."""
    return 3000.0 - 10 * speed


def falling(monkeypatch):
    scanned(monkeypatch, lambda speed, motor: without_peak(speed))


def under_autotune(printer, monkeypatch, driver):
    """klipper_tmc_autotune manages motor A, driven by a TMC`driver`."""
    printer.settings = {'tmc%s stepper_x' % driver: {}, 'autotune_tmc stepper_x': {}}
    stub = collect.detect_hardware
    monkeypatch.setattr(collect, 'detect_hardware', lambda kl, axis: replace(
        stub(kl, axis), driver=tmc.DRIVERS[driver],
        autotune=collect.autotune_goal(printer.settings, 'stepper_' + axis)))


def pasted(line: str):
    """A console line's command, pasted into the console: the run it starts."""
    parser = build_parser()
    words = line.split(': ')[-1].split()
    command = words[0][len('CHOPPER_'):].lower().replace('_', '-')
    return parser.parse_args([command] + _gcode_args(words[1:], boolean_flags(parser)))


@pytest.mark.parametrize('motor', ['a', 'b'])
def test_a_speed_scan_names_the_tune_at_the_resonance_it_found(stub_printer, monkeypatch,
                                                              capsys, motor):
    # the resonance to tune at: the lower peak, not the stronger one at 90 mm/s
    scanned(monkeypatch, lambda speed, _: max(1600.0 - 40 * abs(speed - 40),
                                              2000.0 - 40 * abs(speed - 90), 200.0))
    on_its_own(stub_printer, monkeypatch, find_speed)
    label = motor.upper()
    assert find_speed.run_find_speed(build_parser().parse_args(
        ['find-speed', '--motor', motor, '--yes', '--no-raw'])) == 0
    advice = 'To tune motor %s at its resonance speed: CHOPPER_TUNE MOTOR=%s SPEED=40' % (label, label)
    assert capsys.readouterr().out.splitlines()[-1] == advice
    # after the verdict, which stays on the display
    assert console(stub_printer)[-2:] == ['Chopper: resonance 40 mm/s', advice]
    assert stub_printer.log[-1] == 'RESPOND PREFIX="Chopper:" MSG="%s"' % advice


def test_a_speed_scan_without_a_peak_names_the_tune_where_the_motor_vibrates_most(
        stub_printer, monkeypatch, capsys):
    on_its_own(stub_printer, monkeypatch, find_speed)
    falling(monkeypatch)
    assert find_speed.run_find_speed(build_parser().parse_args(
        ['find-speed', '--motor', 'b', '--yes', '--no-raw'])) == 0
    advice = ('B: no clear resonance peak. To tune motor B at the speed it vibrates most: '
              'CHOPPER_TUNE MOTOR=B SPEED=20')
    assert capsys.readouterr().out.splitlines()[-1] == advice
    assert console(stub_printer)[-1] == advice


@pytest.mark.parametrize('argv, tune_there', [
    (['tune', '--motor', 'a'], 'CHOPPER_TUNE MOTOR=A SPEED=20'),
    # the run's own parameters stay: a save from the panel, a limit set for a whine
    (['tune', '--motor', 'a', '--save', '--audible-khz', '31', '--skip-audible'],
     'CHOPPER_TUNE MOTOR=A SPEED=20 SAVE=1 AUDIBLE_KHZ=31 SKIP_AUDIBLE=1'),
])
def test_a_tune_without_a_peak_names_the_tune_where_the_motor_vibrates_most(
        stub_printer, monkeypatch, capsys, argv, tune_there):
    falling(monkeypatch)
    with pytest.raises(SystemExit) as stopped:
        tune.run_tune(build_parser().parse_args(argv))
    assert stopped.value.code == ('A: no clear resonance peak. To tune motor A at the speed it '
                                  'vibrates most: ' + tune_there)
    # the scan, a stage of the tune, adds no advice of its own
    assert 'CHOPPER_' not in capsys.readouterr().out
    assert not [line for line in console(stub_printer) if 'CHOPPER_' in line]


@pytest.mark.parametrize('argv', [['tune'], ['tune', '--motor', 'ab', '--save']])
@pytest.mark.parametrize('speed', [20, 64, 250])
def test_the_tune_a_failed_scan_names_reaches_the_display_whole(argv, speed):
    # the panel's 2,4 Tune sends MOTOR=AB SAVE=1. KlipperScreen's status line and Mainsail's
    # header show 120 characters of a failure, a 16-character LCD row its first words
    tune_there = find_speed.tune_at('x', speed, build_parser().parse_args(argv))
    shown = collect.failure_display('tune FAILED: ' + find_speed.no_peak('A', tune_there))
    assert shown[:collect.LCD_WIDTH] == 'FAIL A: no clear' and shown.endswith(': ' + tune_there)


@pytest.mark.parametrize('argv, undone', [
    # no peak on motor A: the run stops before motor B
    (['tune'], 'The run did not reach motor B. To tune it: CHOPPER_TUNE MOTOR=B'),
    (['tune', '--save', '--skip-audible'],
     'The run did not reach motor B. To tune it: CHOPPER_TUNE MOTOR=B SAVE=1 SKIP_AUDIBLE=1'),
])
def test_a_tune_that_stops_before_motor_b_names_the_tune_of_motor_b(
        stub_printer, monkeypatch, capsys, argv, undone):
    falling(monkeypatch)
    with pytest.raises(SystemExit, match='^A: no clear resonance peak'):
        tune.run_tune(build_parser().parse_args(argv))
    assert capsys.readouterr().out.splitlines()[-1] == undone
    assert [line for line in console(stub_printer) if 'CHOPPER_' in line] == [undone]
    # pasted, it tunes motor B as the run would have
    assert vars(pasted(undone)) == dict(vars(build_parser().parse_args(argv)), axis='y')


@pytest.mark.parametrize('argv', [['tune'], ['tune', '--save']])
def test_a_tune_that_stops_at_motor_b_names_the_save_of_motor_a(stub_printer, monkeypatch,
                                                                  capsys, argv):
    # motor A has its peak at 60 mm/s and is tuned; motor B has none
    scanned(monkeypatch, lambda speed, motor: without_peak(speed) if motor == 'B'
            else 2000.0 - 30 * abs(speed - 60))
    with pytest.raises(SystemExit, match='^B: no clear resonance peak'):
        tune.run_tune(build_parser().parse_args(argv))
    undone = 'The run tuned motor A. To save it: CHOPPER_SAVE MOTOR=A'
    assert capsys.readouterr().out.splitlines()[-1] == undone
    assert [line for line in console(stub_printer) if 'CHOPPER_' in line] == [undone]
    assert pasted(undone).motor == 'A'
    assert Dataset.open(stub_printer.roots[1]).manifest()['winner']     # motor A's search


def test_a_tune_that_stops_names_no_save_of_what_has_nothing_to_save():
    args = build_parser().parse_args(['tune'])
    tuned = [(search(axis='x', stepper='stepper_x'), None)]
    assert tune.left_undone(args, {}, ['x', 'y'], 'y', tuned) == [
        'The run tuned motor A. To save it: CHOPPER_SAVE MOTOR=A']
    # the motor kept its own registers, or klipper_tmc_autotune writes its own at start
    kept = [(dict(tuned[0][0], kept=True), None)]
    assert tune.left_undone(args, {}, ['x', 'y'], 'y', kept) == []
    assert tune.left_undone(args, {'autotune_tmc stepper_x': {}}, ['x', 'y'], 'y', tuned) == []
    # one motor: nothing else to do
    assert tune.left_undone(args, {}, ['y'], 'y', []) == []


def test_a_tune_names_one_next_step_its_own(stub_printer, capsys):
    tune.run_tune(build_parser().parse_args(['tune', '--no-raw']))
    out = capsys.readouterr().out
    # neither the scan's nor the register search's advice: the tune's save, once
    assert [line for line in out.splitlines() if 'CHOPPER_' in line or 'chopper-autotune ' in line] \
        == ['To save: CHOPPER_SAVE MOTOR=AB']
    said = [line for line in console(stub_printer) if 'CHOPPER_' in line]
    assert len(said) == 1 and said[0].startswith('Tune done: ') \
        and said[0].endswith(' — to save: CHOPPER_SAVE MOTOR=AB')


@pytest.mark.parametrize('motor', ['a', 'b'])
def test_a_register_search_names_how_to_hear_and_save_what_it_found(stub_printer, monkeypatch,
                                                                     capsys, motor):
    on_its_own(stub_printer, monkeypatch, collect)
    label = motor.upper()
    assert collect.run_collect(build_parser().parse_args(
        ['collect', '--motor', motor, '--speed', '58', '--search', 'descent', '--yes',
         '--no-raw'])) == 0
    # each command at the end of its own line: a '.' after one breaks it when pasted
    advice = ['To hear the registers this run found, set them live: CHOPPER_ANALYZE APPLY=1',
              'To save the registers this run found: CHOPPER_SAVE MOTOR=%s' % label]
    out = capsys.readouterr().out
    assert out.splitlines()[-2:] == advice and 'chopper-autotune ' not in out
    assert console(stub_printer)[-2:] == advice
    assert pasted(advice[0]).apply and pasted(advice[1]).motor == label


@pytest.mark.parametrize('driver', ['2209', '2208'])
def test_a_register_search_under_autotune_says_what_autotune_means_instead(
        stub_printer, monkeypatch, capsys, driver):
    on_its_own(stub_printer, monkeypatch, collect)
    under_autotune(stub_printer, monkeypatch, driver)
    assert collect.run_collect(build_parser().parse_args(
        ['collect', '--motor', 'a', '--speed', '58', '--search', 'descent', '--yes',
         '--no-raw'])) == 0
    out = capsys.readouterr().out
    # once, at the end, in the log: saved registers would never reach the driver
    assert out.count('README: With klipper_tmc_autotune') == 1
    assert out.splitlines()[-1].startswith('stepper_x: klipper_tmc_autotune ([autotune_tmc '
                                           'stepper_x]) writes its own')
    assert 'CHOPPER_ANALYZE' not in out
    assert not [line for line in console(stub_printer) if 'CHOPPER_' in line]


@pytest.mark.parametrize('driver', ['2209', '2208'])
def test_a_tune_under_autotune_says_what_autotune_means_once(stub_printer, monkeypatch, capsys,
                                                             driver):
    under_autotune(stub_printer, monkeypatch, driver)
    tune.run_tune(build_parser().parse_args(['tune', '--motor', 'a', '--no-raw']))
    out = capsys.readouterr().out
    # the tune's summary says it: the register search, a stage, adds no advice of its own
    assert out.count('README: With klipper_tmc_autotune') == 1
    assert 'stepper_x: klipper_tmc_autotune ([autotune_tmc stepper_x]) writes its own' in out


def test_a_command_run_dry_names_no_next_step(stub_printer, monkeypatch, capsys):
    # DRY_RUN=1 only plans: no speed found, no registers to hear or save
    for module, argv in ((find_speed, ['find-speed', '--dry-run']),
                         (collect, ['collect', '--speed', '58', '--search', 'descent',
                                    '--dry-run'])):
        on_its_own(stub_printer, monkeypatch, module)
        run = find_speed.run_find_speed if module is find_speed else collect.run_collect
        assert run(build_parser().parse_args(argv)) == 0
        assert 'CHOPPER_' not in capsys.readouterr().out
    assert stub_printer.log == []


def search(**fields) -> dict:
    """The manifest of a register search, on motor B unless `fields` name another."""
    return {'axis': 'y', 'stepper': 'stepper_y', 'driver': '2209', **fields}


def searched(tmp_path, **fields) -> Dataset:
    ds = Dataset.create(tmp_path / 'run', search(**fields))
    ds.append({'id': 'a', 'kind': 'move', 'status': 'ok', **tmc.Chopper(0, 5, 0, 6).fields(),
               'score': {'median_magnitude': 100.0}})
    return ds


def test_a_register_search_names_no_next_step_where_there_is_nothing_to_hear_or_save(tmp_path):
    winner = tmc.Chopper(0, 5, 0, 6).fields()
    assert collect.hear_and_save({}, searched(tmp_path, winner=winner))[-1].endswith(
        'CHOPPER_SAVE MOTOR=B')
    # a full sweep with VALIDATE=0 records no winner: CHOPPER_SAVE says how to save it anyway
    assert collect.hear_and_save({}, searched(tmp_path / 'grid'))[-1].endswith(
        'CHOPPER_SAVE MOTOR=B')
    # the motor keeps its own registers: nothing new to hear or write
    assert collect.hear_and_save({}, searched(tmp_path / 'kept', winner=winner, kept=True)) == []
    # klipper_tmc_autotune writes its own chopper at every start
    assert collect.hear_and_save({'autotune_tmc stepper_y': {}},
                                   searched(tmp_path / 'autotune', winner=winner)) == []
    empty = Dataset.create(tmp_path / 'empty', {'axis': 'y', 'stepper': 'stepper_y'})
    empty.append({'id': 'a', 'kind': 'move', 'status': 'failed'})
    assert collect.hear_and_save({}, empty) == []


class Guard:
    def __init__(self, *args):
        pass

    def preflight(self):
        pass

    def check(self):
        pass


class Config:
    """Moonraker over a config whose motors run at `amps`: the g-code it sends and the files
    it uploads, in order."""

    def __init__(self, amps, files=None):
        self.files = files or {'printer.cfg': ''.join(
            '[tmc2209 stepper_%s]\nrun_current: %s\n\n' % (m, value) for m, value in zip('xy', amps))}
        self.events = []

    def is_printing(self):
        return False

    def accepted_commands(self):
        return None

    def list_config_files(self):
        return list(self.files)

    def download_config(self, name):
        return self.files[name]

    def upload_config(self, name, content):
        self.events.append('upload ' + name)
        self.files[name] = content

    def gcode(self, script):
        self.events.append(script)

    def console(self) -> 'list[str]':
        return [event.split(' MSG="', 1)[1][:-1] for event in self.events
                if event.startswith('RESPOND ')]


def run_current(monkeypatch, argv, kinematics='corexy', configured=(1.2, 1.2), autotune=(),
                config=None):
    """CHOPPER_CURRENT on a printer whose motors skip below 0.4 A: its final message, its
    display line and the config Moonraker saw."""
    scripts, rung = [], {}
    hw = SimpleNamespace(kinematics=kinematics, axis_span=300.0, max_accel=3000.0,
                         center=(150.0, 150.0), display=True, driver=SimpleNamespace(name='2209'))

    class Referee:
        def __init__(self, *args):
            pass

        def calibrate(self):
            pass

        def slipped(self):
            return 0.0 if rung['current'] >= 0.4 else None

    status = {'toolhead': {'max_velocity': 300.0, 'max_accel': 3000.0, 'minimum_cruise_ratio': 0.5},
              'gcode_move': {'speed_factor': 1.0},
              'gcode': {'commands': {'M117': {}, 'M118': {}, 'RESPOND': {}}}}
    settings = {'tmc2209 stepper_%s' % m: {'run_current': amps} for m, amps in zip('xy', configured)}
    settings.update({'autotune_tmc stepper_' + m: {} for m in autotune})
    kl = SimpleNamespace(gcode=scripts.append, request=lambda method, params=None: {'status': status},
                         settings=lambda: settings)
    for name, value in (('detect_hardware', lambda kl, axis, accel=False: hw),
                        ('run_rung', lambda kl, board, m, amps, *more: rung.update(current=amps)),
                        ('Referee', Referee), ('ThermalGuard', Guard),
                        ('home_xy', lambda *args: None), ('refuse_blind_z_hop', lambda *args: None),
                        ('refuse_if_printing', lambda *args: None),
                        ('rehome_unless_hot', lambda *args: None)):
        monkeypatch.setattr(current, name, value)
    mk = config or Config(configured)
    monkeypatch.setattr(moonraker, 'Moonraker', lambda url: mk)
    try:
        assert current.current_tune(kl, build_parser().parse_args(argv + ['--yes'])) == 0
    finally:
        final = [script[len('M118 '):] for script in scripts if script.startswith('M118 ')]
        shown = [script[len('M117 '):] for script in scripts if script.startswith('M117 ')]
        mk.final, mk.shown = final[-1], shown[-1]
    return mk.final, mk.shown, mk


def test_a_saved_current_names_the_tune_again_once_it_writes(monkeypatch, capsys):
    final, shown, mk = run_current(monkeypatch, ['current', '--save'])
    assert mk.files['printer.cfg'].count('run_current: 0.82') == 2
    # the final message comes before the write, which may still fail: it names no tune
    assert final.endswith(' — saving')
    tune_again = 'The chopper optimum depends on the run current, so tune again: CHOPPER_TUNE SAVE=1'
    assert mk.console() == [tune_again]
    assert mk.events.index('RESPOND PREFIX="Chopper:" MSG="%s"' % tune_again) \
        < mk.events.index('upload printer.cfg')
    assert tune_again in capsys.readouterr().out.splitlines()
    assert shown == 'A 0.82A B 0.82A'                     # the display keeps the currents
    # only a motor whose current changed is tuned again
    _, _, mk = run_current(monkeypatch, ['current', '--save'], 'cartesian', (1.2, 0.8))
    assert 'stepper_y]\nrun_current: 0.8\n' in mk.files['printer.cfg']
    assert mk.console()[-1].endswith(': CHOPPER_TUNE MOTOR=A SAVE=1')


def test_a_current_the_config_cannot_take_names_no_tune(monkeypatch):
    # Klipper merges a section declared in two files; the tool refuses to guess which to edit
    twice = Config((1.2, 1.2))
    twice.files['steppers.cfg'] = '[tmc2209 stepper_x]\nrun_current: 1.2\n'
    twice.files['printer.cfg'] = '[include steppers.cfg]\n' + twice.files['printer.cfg']
    with pytest.raises(SystemExit, match='found in several files'):
        run_current(monkeypatch, ['current', '--save'], config=twice)
    assert twice.final.endswith(' — saving')
    assert 'CHOPPER_TUNE' not in twice.final and twice.console() == []
    assert not [event for event in twice.events if event.startswith('upload ')]


def test_a_current_under_autotune_names_no_tune_of_its_motor(monkeypatch):
    # klipper_tmc_autotune writes its own chopper at every start: TUNE SAVE=1 refuses there
    _, _, mk = run_current(monkeypatch, ['current', '--save'], autotune='x')
    assert mk.console() == ['The chopper optimum depends on the run current, so tune again: '
                            'CHOPPER_TUNE MOTOR=B SAVE=1']
    _, _, mk = run_current(monkeypatch, ['current', '--save'], autotune='xy')
    assert mk.files['printer.cfg'].count('run_current: 0.82') == 2 and mk.console() == []


@pytest.mark.parametrize('argv, command', [
    (['current'], 'CHOPPER_CURRENT SAVE=1'),
    (['current', '--motor', 'b', '--margin', '2.5', '--min-current', '0.35', '--resolution',
      '0.02', '--accel', '2500'],
     'CHOPPER_CURRENT MOTOR=B MARGIN=2.5 MIN_CURRENT=0.35 RESOLUTION=0.02 ACCEL=2500 SAVE=1'),
    (['current', '--motor', 'ab', '--per-motor'], 'CHOPPER_CURRENT PER_MOTOR=1 SAVE=1'),
])
def test_a_current_not_saved_names_the_same_run_with_save(monkeypatch, capsys, argv, command):
    final, _, mk = run_current(monkeypatch, argv)
    # it measures again: the command runs the whole pattern, not a write of this result
    assert final.endswith(' — to measure again and save: ' + command)
    assert mk.events == []
    assert capsys.readouterr().out.splitlines()[-1] == 'To save, measure again: ' + command
    # pasted into the console, it is this very run with SAVE=1
    assert vars(pasted(command)) == dict(vars(build_parser().parse_args(argv)), save=True)


def test_a_current_that_stays_names_no_next_step(monkeypatch, capsys):
    for argv in (['current'], ['current', '--save']):
        final, _, mk = run_current(monkeypatch, argv, configured=(0.8, 0.8))
        assert mk.events == [] and 'CHOPPER_' not in final
        assert capsys.readouterr().out.splitlines()[-1] == \
            'Nothing to save: recommended current equals the configured one'


@pytest.mark.parametrize('argv', [
    ['tune'],
    ['tune', '--motor', 'b', '--speed', '40:70', '--save', '--iterations', '2', '--audible-khz',
     '31.5', '--audible-weight', '0', '--no-skip-audible', '--accel', '2500', '--no-raw', '--csv'],
    ['current', '--motor', 'a', '--margin', '2.5', '--per-motor', '--save', '--dry-run'],
    ['save', '--motor', 'be', '--skip-audible', '--url', 'http://printer:7125'],
])
def test_a_command_to_paste_is_the_run_it_names(argv):
    from chopper_autotune.cli import command_line
    args = build_parser().parse_args(argv)
    line = command_line(argv[0], args)
    assert line.split()[0] == 'CHOPPER_' + argv[0].upper()
    assert pasted(line) == args
