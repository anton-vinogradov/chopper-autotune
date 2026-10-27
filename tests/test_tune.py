import builtins
import sys
import time
from types import SimpleNamespace

import pytest

from chopper_autotune import tmc, tune
from chopper_autotune.cli import build_parser
from chopper_autotune.dataset import Dataset
from chopper_autotune.moonraker import MoonrakerError


@pytest.fixture(autouse=True)
def two_wheel_drive(monkeypatch):
    # tune refuses multi-motor axes first thing; these tests run a 2WD config
    monkeypatch.setattr(tune.Klippy, 'settings', lambda self: {})


def moonraker(settings: dict, **more) -> SimpleNamespace:
    """Moonraker of a printer whose printer.cfg gives the sections of `settings`, the ones
    Klipper loaded from it."""
    def download_config(name):
        return ''.join('[%s]\n%s\n' % (section, ''.join('%s: %s\n' % option
                                                       for option in options.items()))
                       for section, options in settings.items())
    return SimpleNamespace(settings=lambda: settings, accepted_commands=lambda: None,
                           list_config_files=lambda: ['printer.cfg'],
                           download_config=download_config, **more)


def tune_args(**overrides):
    # through the real parser, so a new parameter of CHOPPER_TUNE reaches these runs
    args = build_parser().parse_args(['tune', '--url', 'http://x'])
    for name, value in overrides.items():
        setattr(args, name, value)
    return args


def test_a_tune_at_a_terminal_runs_its_stages_without_asking(stub_printer, monkeypatch):
    # CHOPPER_TUNE asks nothing before it moves, so neither do its scan and its search
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(builtins, 'input', lambda prompt: pytest.fail('asked: ' + prompt))
    assert tune.run_tune(build_parser().parse_args(['tune', '--motor', 'a', '--no-raw'])) == 0
    assert 'MOVE' in stub_printer.log and stub_printer.measured


def make_dataset(tmp_path, axis, toff, winner=True):
    combo = tmc.Chopper(0, toff, 7, 5)
    ds = Dataset.create(tmp_path / axis, {
        'axis': axis, 'search': 'descent', 'driver': '2209', 'stepper': 'stepper_' + axis,
        'trim': 0.1, **({'winner': combo.fields()} if winner else {})})
    ds.append({'id': 'a', 'kind': 'move', 'status': 'ok', **combo.fields(),
               'score': {'median_magnitude': 1000.0}})
    return str(ds.root)


def test_tune_runs_both_axes_and_seeds_second(tmp_path, monkeypatch, capsys):
    calls = {'scan': [], 'collect': [], 'popups': []}
    roots = {'x': make_dataset(tmp_path, 'x', 8), 'y': make_dataset(tmp_path, 'y', 6)}

    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)

    def fake_scan(kl, args, popup):
        calls['scan'].append(args.axis)
        calls['popups'].append(popup)
        speed = {'x': 58, 'y': 52}[args.axis]
        return 0, speed, speed

    def fake_collect(kl, args, popup):
        calls['collect'].append((args.axis, args.speed, args.seed_from))
        calls['popups'].append(popup)
        return 0, roots[args.axis]

    monkeypatch.setattr(tune, 'scan', fake_scan)
    monkeypatch.setattr(tune, 'collect', fake_collect)

    assert tune.run_tune(tune_args()) == 0
    assert calls['scan'] == ['x', 'y']
    assert calls['collect'] == [('x', tmc.Range(58, 58), None),
                                ('y', tmc.Range(52, 52), roots['x'])]
    # the motors still have work after each stage: only the tune's own verdict pops up
    assert calls['popups'] == [False] * 4
    out = capsys.readouterr().out
    assert '[tmc2209 stepper_x]' in out and 'driver_TOFF: 8' in out
    assert '[tmc2209 stepper_y]' in out and 'driver_TOFF: 6' in out
    # one way to save this run: the command for its motors
    assert 'To save: CHOPPER_SAVE MOTOR=AB\n' in out and 'CHOPPER_ANALYZE' not in out


def test_tune_single_axis_with_explicit_speed(tmp_path, monkeypatch):
    roots = {'y': make_dataset(tmp_path, 'y', 5)}
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: pytest.fail('scan must be skipped'))
    monkeypatch.setattr(tune, 'collect', lambda kl, args, popup: (0, roots[args.axis]))

    assert tune.run_tune(tune_args(axis='y', speed=52)) == 0


def test_the_summary_gives_the_gain_over_klipper_defaults_with_two_decimals(tmp_path, monkeypatch,
                                                                            capsys):
    # one decimal printed this gain as 1.0x
    root = make_dataset(tmp_path, 'y', 5)
    Dataset.open(root).update_manifest(improvement=1.04)
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'Moonraker', lambda url: moonraker({}))
    monkeypatch.setattr(tune, 'collect', lambda kl, args, popup: (0, root))
    finals = []
    monkeypatch.setattr(tune.Screen, 'final', lambda self, text, short=None: finals.append(text))
    assert tune.run_tune(tune_args(axis='y', speed=52)) == 0
    assert '\ndriver_HEND: 5\n# 1.04x less vibration than Klipper defaults\n' \
        in capsys.readouterr().out
    assert finals[-1].startswith('Tune done: B 0/5/7/5 -4%')


def test_tune_save_saves_what_save_with_the_motors_of_the_run_saves(tmp_path, monkeypatch):
    # one way to save a run: SAVE=1 now is CHOPPER_SAVE MOTOR=<its motors> later
    from pathlib import Path
    from types import SimpleNamespace

    from chopper_autotune import analyze, extruder
    from chopper_autotune.cli import build_parser
    from chopper_autotune.dataset import save_json
    roots = {'x': make_dataset(tmp_path, 'x', 8), 'y': make_dataset(tmp_path, 'y', 6)}
    extruder.save_winner_state('2209', tmc.Chopper(3, 7, 6, 0))    # not a motor of these runs
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: [Path(roots[axis]) for axis in 'xy'])
    # the config holds other registers than the runs found
    settings = {'tmc2209 stepper_' + axis: {'driver_toff': 3} for axis in 'xy'}
    told = []
    mk = moonraker(settings, gcode=told.append)
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    monkeypatch.setattr(tune, 'Moonraker', lambda url: mk)
    saved = []
    monkeypatch.setattr(analyze, 'run_save', lambda mk, items, extruder_state=None, lines=():
                        saved.append(([manifest['stepper'] for manifest, _ in items], extruder_state,
                                      lines)))
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: (0, 58, 58))
    monkeypatch.setattr(tune, 'collect', lambda kl, args, popup: (0, roots[args.axis]))

    # a run of motor A alone leaves B's newer result unsaved
    for axis, motor, steppers in (('xy', 'AB', ['stepper_x', 'stepper_y']), ('x', 'A', ['stepper_x'])):
        tune.run_tune(tune_args(axis=axis, save=True))
        analyze.run_save_latest(build_parser().parse_args(['save', '--motor', motor]))
        assert len(saved) == 2 and saved[0] == saved[1] and saved[0][:2] == (steppers, None)
        # the runs recorded TBL 0, an edge the default search leaves out
        assert saved[0][2][0].startswith('motor A: saving tbl0_toff8_hstrt7_hend5 from run x. These '
                                         'registers have TBL 0, an edge')
        assert saved[0][2][0].endswith('To search without this edge: CHOPPER_TUNE MOTOR=A')
        saved.clear()
    # a clock set back since the last CHOPPER_RESTORE (a board without RTC or network):
    # the run just made is newer than it all the same
    save_json(analyze.RESULTS_HOME / analyze.RESTORED, {'time': time.time() + 3600})
    tune.run_tune(tune_args(save=True))
    assert [steppers for steppers, _, _ in saved] == [['stepper_x', 'stepper_y']]
    saved.clear()
    # a motor that keeps its own registers has nothing to write: no backup, no restart
    settings['tmc2209 stepper_y'] = {'driver_tbl': 0, 'driver_toff': 6, 'driver_hstrt': 7, 'driver_hend': 5}
    Dataset.open(roots['y']).update_manifest(kept=True)
    tune.run_tune(tune_args(save=True))
    assert [steppers for steppers, _, _ in saved] == [['stepper_x']]
    settings['tmc2209 stepper_x'] = {'driver_tbl': 0, 'driver_toff': 8, 'driver_hstrt': 7, 'driver_hend': 5}
    Dataset.open(roots['x']).update_manifest(kept=True)
    saved.clear()
    tune.run_tune(tune_args(save=True))
    assert saved == [] and told and 'Nothing to save' in told[-1]


def test_a_tune_whose_winners_the_config_already_has_writes_nothing(tmp_path, monkeypatch, capsys):
    # the run ended on the registers in the config without marking them kept: same lines,
    # a copy of the config and a restart would buy nothing
    from pathlib import Path
    from types import SimpleNamespace

    from chopper_autotune import analyze
    roots = {'x': make_dataset(tmp_path, 'x', 8), 'y': make_dataset(tmp_path, 'y', 6)}
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: [Path(roots[axis]) for axis in 'xy'])
    settings = {'tmc2209 stepper_' + axis: {'driver_tbl': 0, 'driver_toff': toff, 'driver_hstrt': 7,
                                            'driver_hend': 5} for axis, toff in (('x', 8), ('y', 6))}
    monkeypatch.setattr(tune.Klippy, 'settings', lambda self: settings)
    told = []
    mk = moonraker(settings, gcode=told.append,
                   upload_config=lambda *args: pytest.fail('nothing to write'))
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    monkeypatch.setattr(tune, 'Moonraker', lambda url: mk)
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: (0, 58, 58))
    monkeypatch.setattr(tune, 'collect', lambda kl, args, popup: (0, roots[args.axis]))
    finals = []
    monkeypatch.setattr(tune.Screen, 'final', lambda self, text, short=None: finals.append(text))
    for save in (False, True):
        capsys.readouterr()
        assert tune.run_tune(tune_args(save=save)) == 0
        assert finals[-1].endswith(' — your registers stay')
        assert 'To save' not in capsys.readouterr().out
    assert told == ['\n'.join('RESPOND PREFIX="Chopper:" MSG="%s"' % line for line in (
        'motor A: the config already has tbl0_toff8_hstrt7_hend5, the result of run x, so there is '
        'nothing to write',
        'motor B: the config already has tbl0_toff6_hstrt7_hend5, the result of run y, so there is '
        'nothing to write', 'Nothing to save, Klipper keeps running'))]
    # one register away from the winner: motor B has something to write, motor A has not
    settings['tmc2209 stepper_y']['driver_hend'] = 4
    assert tune.run_tune(tune_args()) == 0
    assert finals[-1].endswith(' — to save: CHOPPER_SAVE MOTOR=B')
    assert capsys.readouterr().out.splitlines()[-1] == 'To save: CHOPPER_SAVE MOTOR=B'


def test_winner_of_prefers_the_recorded_winner(tmp_path):
    # the run's validated recommendation, not a full re-rank that can surface
    # an unvalidated lucky combo (winner's curse)
    root = make_dataset(tmp_path, 'x', 8)
    Dataset.open(root).update_manifest(winner={'tbl': 2, 'toff': 1, 'hstrt': 4, 'hend': 14})
    _, combo = tune.winner_of(root, 0.25)
    assert combo == tmc.Chopper(2, 1, 4, 14)


def test_winner_of_falls_back_to_ranking(tmp_path):
    root = make_dataset(tmp_path, 'x', 8, winner=False)
    _, combo = tune.winner_of(root, 0.25)
    assert combo == tmc.Chopper(0, 8, 7, 5)


def test_tune_aborts_without_resonance_peak(monkeypatch):
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: (0, None, 64))

    with pytest.raises(SystemExit, match='no clear resonance peak'):
        tune.run_tune(tune_args())


def test_tune_reports_the_outcome_on_screen(tmp_path, monkeypatch, capsys):
    """The run's outcome must reach the display: the console summary only lands in the
    detached log, and SAVE=1 restarts Klipper, wiping the status line (field-reported:
    "tune ended and nothing said how")."""
    roots = {'x': make_dataset(tmp_path, 'x', 8), 'y': make_dataset(tmp_path, 'y', 2)}
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: (0, 58, 58))
    monkeypatch.setattr(tune, 'collect', lambda kl, args, popup: (0, roots[args.axis]))
    finals = []

    class FakeScreen:
        def __init__(self, kl, display):
            pass

        def final(self, text, short=None):
            finals.append(text)

    monkeypatch.setattr(tune, 'Screen', FakeScreen)
    assert tune.run_tune(tune_args()) == 0
    assert finals and finals[-1].startswith('Tune done: A 0/8/7/5 · B 0/2/7/5')
    assert finals[-1].endswith(' — to save: CHOPPER_SAVE MOTOR=AB')
    assert tune.run_tune(tune_args(axis='y', speed=52)) == 0
    assert finals[-1].endswith(' — to save: CHOPPER_SAVE MOTOR=B')
    # a motor whose own registers held against the winner says so: the config Klipper loaded
    # gave them
    loaded = {'tmc2209 stepper_' + axis: {'driver_tbl': 0, 'driver_toff': toff, 'driver_hstrt': 7,
                                          'driver_hend': 5} for axis, toff in (('x', 8), ('y', 2))}
    monkeypatch.setattr(tune.Klippy, 'settings', lambda self: loaded)
    files = dict(loaded)
    monkeypatch.setattr(tune, 'Moonraker', lambda url: moonraker(files))
    Dataset.open(roots['y']).update_manifest(kept=True, winner={'tbl': 0, 'toff': 2, 'hstrt': 7,
                                                                'hend': 5})
    assert tune.run_tune(tune_args()) == 0
    assert finals[-1].startswith('Tune done: A 0/8/7/5 · B keep 0/2/7/5')
    Dataset.open(roots['x']).update_manifest(kept=True)
    capsys.readouterr()
    assert tune.run_tune(tune_args()) == 0
    assert finals[-1].endswith(' — your registers stay')
    assert 'To save' not in capsys.readouterr().out          # nor does the log offer a save
    # Moonraker does not give the files: the config Klipper loaded decides, and the tune
    # ends with its verdict all the same
    def unread():
        raise MoonrakerError('cannot reach Moonraker at http://x: [Errno 61] Connection refused')
    monkeypatch.setattr(tune, 'Moonraker', lambda url: SimpleNamespace(
        **dict(vars(moonraker(files)), list_config_files=unread)))
    assert tune.run_tune(tune_args()) == 0
    assert finals[-1].endswith(' — your registers stay')
    assert 'cannot reach Moonraker at http://x' in capsys.readouterr().out
    monkeypatch.setattr(tune, 'Moonraker', lambda url: moonraker(files))
    # Klipper runs them, but its next start reads others from the files (an old copy moved,
    # a hand edit): CHOPPER_SAVE writes them, so the tune names it
    files['tmc2209 stepper_y'] = {'driver_toff': 3}
    assert tune.run_tune(tune_args()) == 0
    assert finals[-1].endswith(' — to save: CHOPPER_SAVE MOTOR=B')
    assert capsys.readouterr().out.splitlines()[-1] == 'To save: CHOPPER_SAVE MOTOR=B'

    # a failure must speak too — via the global CLI announcer now (any tool, any error)
    finals.clear()
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: (0, None, 64))
    with pytest.raises(SystemExit, match='no clear resonance peak'):
        tune.run_tune(tune_args())


def test_cli_announces_failures_on_the_display(monkeypatch):
    """The tools run detached: an error that only lands in the log looks like
    'nothing starts' at the machine (field: a missing accelerometer died silently)."""
    import chopper_autotune.cli as cli
    said = []
    monkeypatch.setattr(cli, 'announce_failure', lambda args, msg: said.append(msg))

    def boom(args):
        raise SystemExit('accelerometer unplugged')

    monkeypatch.setattr(cli, 'dispatch', boom)
    with pytest.raises(SystemExit, match='accelerometer unplugged'):
        cli.main(['status'])
    assert said == ['status FAILED: accelerometer unplugged']

    said.clear()

    def stop(args):
        raise SystemExit(143)                       # CHOPPER_STOP's integer code

    monkeypatch.setattr(cli, 'dispatch', stop)
    with pytest.raises(SystemExit):
        cli.main(['status'])
    assert said == []                               # a plain code is not a message


def test_improvement_note_formats_the_quieter_factor():
    from chopper_autotune.tune import improvement_note
    assert improvement_note({'improvement': 1.85}) == ' -46%'
    assert improvement_note({'improvement': 1.0}) == ''      # nothing won
    assert improvement_note({'improvement': 1.003}) == ''     # a statistical tie is not -0%
    assert improvement_note({'improvement': 0.9}) == ''      # defaults measured quieter
    assert improvement_note({}) == ''                        # reference never measured


def test_save_on_an_autotune_motor_is_refused_before_tuning(monkeypatch):
    # klipper_tmc_autotune rewrites the chopper at every start: SAVE=1 could not work,
    # so say it now instead of after twenty minutes of tuning
    monkeypatch.setattr(tune.Klippy, 'settings', lambda self: {
        'tmc2240 stepper_y': {}, 'autotune_tmc stepper_y': {'motor': 'ldo-42sth48-2004ac'}})
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: pytest.fail('nothing may run'))
    with pytest.raises(SystemExit, match=r'not saving \[tmc2240 stepper_y\]'):
        tune.run_tune(tune_args(save=True))
    # without SAVE=1 the measurement itself is still worth running
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: (_ for _ in ()).throw(RuntimeError('ran')))
    with pytest.raises(RuntimeError, match='ran'):
        tune.run_tune(tune_args())


def test_a_run_under_autotune_advises_instead_of_offering_to_paste(tmp_path, monkeypatch, capsys):
    # pasting the snippet would change nothing: autotune writes its own chopper at start
    ds = Dataset.create(tmp_path / 'x', {'driver': '2209', 'stepper': 'stepper_x', 'trim': 0.1,
                                         'autotune': 'performance'})
    ds.append({'id': 'a', 'kind': 'move', 'status': 'ok', **tmc.Chopper(0, 8, 7, 5).fields(),
               'score': {'median_magnitude': 1000.0}})
    monkeypatch.setattr(tune.Klippy, 'settings', lambda self: {
        'tmc2209 stepper_x': {}, 'autotune_tmc stepper_x': {'sg4_thrs': 80}})
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: (0, 58, 58))
    monkeypatch.setattr(tune, 'collect', lambda kl, args, popup: (0, str(ds.root)))
    assert tune.run_tune(tune_args(axis='x')) == 0
    out = capsys.readouterr().out
    assert 'driver_SGTHRS: 80' in out and 'tune again' in out
    assert 'paste it manually' not in out


def test_autotune_on_one_motor_marks_only_that_one(tmp_path, monkeypatch, capsys):
    # Save still persists the other motor: its hint must not go away
    roots = {}
    for axis, autotune in (('x', 'performance'), ('y', None)):
        ds = Dataset.create(tmp_path / axis, {'driver': '2209', 'stepper': 'stepper_' + axis,
                                              'trim': 0.1, 'autotune': autotune})
        ds.append({'id': 'a', 'kind': 'move', 'status': 'ok', **tmc.Chopper(0, 8, 7, 5).fields(),
                   'score': {'median_magnitude': 1000.0}})
        roots[axis] = str(ds.root)
    monkeypatch.setattr(tune.Klippy, 'settings', lambda self: {
        'tmc2209 stepper_x': {}, 'autotune_tmc stepper_x': {'sg4_thrs': 80}, 'tmc2209 stepper_y': {}})
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: (0, 58, 58))
    monkeypatch.setattr(tune, 'collect', lambda kl, args, popup: (0, roots[args.axis]))
    finals = []
    monkeypatch.setattr(tune.Screen, 'final', lambda self, text, short=None: finals.append(text))
    assert tune.run_tune(tune_args()) == 0
    out = capsys.readouterr().out
    assert 'stepper_x: klipper_tmc_autotune' in out and 'driver_SGTHRS: 80' in out
    assert 'stepper_y: klipper_tmc_autotune' not in out
    assert 'To save: CHOPPER_SAVE MOTOR=B\n' in out           # Save skips motor A
    assert out.count('driver_TOFF: 8') == 1          # a snippet for B only, advice for A
    assert '(autotune)' in finals[-1] and finals[-1].count('(autotune)') == 1
    assert finals[-1].endswith('to save: CHOPPER_SAVE MOTOR=B')


def test_a_tmc2208_under_autotune_gets_the_advice_not_a_paste(tmp_path, monkeypatch, capsys):
    # no CoolStep, so no 'measured under' tag: yet autotune still writes its own at start
    ds = Dataset.create(tmp_path / 'x', {'driver': '2208', 'stepper': 'stepper_x', 'trim': 0.1,
                                         'autotune': None})
    ds.append({'id': 'a', 'kind': 'move', 'status': 'ok', **tmc.Chopper(0, 8, 7, 5).fields(),
               'score': {'median_magnitude': 1000.0}})
    monkeypatch.setattr(tune.Klippy, 'settings', lambda self: {
        'tmc2208 stepper_x': {}, 'autotune_tmc stepper_x': {}})
    monkeypatch.setattr(tune, 'find_socket', lambda explicit=None: '<sock>')
    monkeypatch.setattr(tune.Klippy, 'connect', lambda self, sock=None: self)
    monkeypatch.setattr(tune.Klippy, 'close', lambda self: None)
    monkeypatch.setattr(tune, 'scan', lambda kl, args, popup: (0, 58, 58))
    monkeypatch.setattr(tune, 'collect', lambda kl, args, popup: (0, str(ds.root)))
    assert tune.run_tune(tune_args(axis='x')) == 0
    out = capsys.readouterr().out
    assert 'tune it again with SAVE=1, or save a result already measured' in out
    assert 'To save' not in out and 'driver_TOFF' not in out
