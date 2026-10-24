"""run.sh, the launcher behind every CHOPPER_* macro. gcode_shell_command ignores
exit codes, so a failure reaches the user only as a printed line: an install that
never created the venv (#130) or a start-up crash must print ERROR, not 'started'."""
import os
import shutil
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
needs_setsid = pytest.mark.skipif(shutil.which('setsid') is None, reason='setsid (util-linux) not available')


def install(tmp_path, body=None):
    """A copy of the launchers with an optional fake .venv/bin/chopper-autotune."""
    root = tmp_path / 'chopper-autotune'
    root.mkdir()
    for name in ('run.sh', 'status.sh', 'tune.sh', 'analyze.sh'):
        shutil.copy(os.path.join(REPO, name), root / name)
    if body is not None:
        binary = root / '.venv' / 'bin' / 'chopper-autotune'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/bash\n' + body)
        binary.chmod(0o755)
    return root


def launch(root, tmp_path, *args):
    env = dict(os.environ, HOME=str(tmp_path))
    started = time.monotonic()
    done = subprocess.run(['bash', str(root / args[0])] + list(args[1:]), env=env,
                          capture_output=True, text=True, timeout=30)
    return done, time.monotonic() - started


def test_missing_install_is_an_error_not_started(tmp_path):
    root = install(tmp_path)
    for script in ('tune.sh', 'status.sh', 'analyze.sh'):
        done, _ = launch(root, tmp_path, script)
        assert done.returncode == 1
        assert 'ERROR: chopper-autotune is not installed' in done.stdout
        assert 'started' not in done.stdout


def test_sync_mode_passes_output_and_status_through(tmp_path):
    # CHOPPER_SYNC stops the tool from reporting through Klipper while this very macro
    # holds the gcode queue (the M117 would wait out the macro timeout)
    root = install(tmp_path, 'echo "status table for $1, sync=$CHOPPER_SYNC"; exit 3\n')
    done, _ = launch(root, tmp_path, 'status.sh')
    assert done.returncode == 3
    assert 'status table for status, sync=1' in done.stdout


@needs_setsid
def test_broken_environment_points_to_the_installer(tmp_path):
    root = install(tmp_path, 'echo "ModuleNotFoundError: No module named \'chopper_autotune\'" >&2; exit 1\n')
    done, _ = launch(root, tmp_path, 'tune.sh')
    assert done.returncode == 1
    assert 'The Python environment is broken' in done.stdout
    assert 'install.sh' in done.stdout


@needs_setsid
def test_start_up_failure_is_reported_with_the_log_tail(tmp_path):
    root = install(tmp_path, 'echo "error: unrecognized arguments: --bogus" >&2; exit 2\n')
    done, _ = launch(root, tmp_path, 'tune.sh', '--bogus')
    assert done.returncode == 2
    assert 'ERROR: chopper-autotune tune exited with status 2' in done.stdout
    assert 'unrecognized arguments: --bogus' in done.stdout
    assert 'started' not in done.stdout


@needs_setsid
def test_quick_clean_exit_is_reported_as_finished(tmp_path):
    root = install(tmp_path, 'echo "dry run: 12 moves planned"; exit 0\n')
    done, _ = launch(root, tmp_path, 'tune.sh', 'DRY_RUN=1')
    assert done.returncode == 0
    assert 'chopper-autotune tune finished' in done.stdout
    assert 'dry run: 12 moves planned' in done.stdout


@needs_setsid
def test_long_run_is_detached_and_reported_as_started(tmp_path):
    marker = tmp_path / 'still-running'
    root = install(tmp_path, 'sleep 3; touch "%s"\n' % marker)
    done, elapsed = launch(root, tmp_path, 'tune.sh')
    assert done.returncode == 0
    assert 'chopper-autotune tune started (PID' in done.stdout
    assert elapsed < 2.5 and not marker.exists()
    log = tmp_path / 'printer_data' / 'config' / 'chopper-autotune' / 'tune.log'
    assert log.exists()


def test_the_cli_parses_arguments_before_numpy_loads():
    # a bad parameter must fail inside the launcher's one-second window even on a
    # slow host: parsing may not wait for numpy
    code = 'import sys, chopper_autotune.cli; print("numpy" in sys.modules)'
    done = subprocess.run([sys.executable, '-c', code], cwd=REPO, capture_output=True, text=True)
    assert done.stdout.strip() == 'False', done.stderr


def test_no_failure_report_through_klipper_in_a_foreground_run(monkeypatch):
    from chopper_autotune import cli, klippy
    calls = []
    monkeypatch.setattr(klippy, 'find_socket', lambda *args: calls.append(args) or '/nonexistent')
    monkeypatch.setenv('CHOPPER_SYNC', '1')
    cli.announce_failure(None, 'status FAILED: no datasets found')
    assert calls == []
    monkeypatch.delenv('CHOPPER_SYNC')
    cli.announce_failure(None, 'tune FAILED: no accelerometer')     # a detached run does report
    assert calls == [(None,)]


def launched(tmp_path, script: str, args: 'list[str]') -> 'list[str]':
    """What `script` hands run.sh for the macro's `args`."""
    shutil.copy(os.path.join(REPO, script), str(tmp_path))
    (tmp_path / 'run.sh').write_text('#!/bin/bash\necho "$@" > "$(dirname "$0")/called"\n')
    os.chmod(str(tmp_path / 'run.sh'), 0o755)
    subprocess.run(['bash', str(tmp_path / script)] + args, check=True)
    return (tmp_path / 'called').read_text().split()


@pytest.mark.parametrize('args, detached', [
    (['APPLY=1'], True), (['apply=On'], True), (['SAVE=yes'], True), (['--apply'], True),
    # APPLY=0 is no apply: the table stays in the console
    (['APPLY=0'], False), (['SAVE=off'], False), (['TOP=5'], False), ([], False),
])
def test_analyze_detaches_only_an_apply_or_a_save(tmp_path, args, detached):
    # APPLY/SAVE send G-code back while RUN_SHELL_COMMAND holds Klipper's queue: detached
    called = launched(tmp_path, 'analyze.sh', args)
    assert (called[0] != '--sync') is detached and called[called.index('analyze'):] == ['analyze'] + args


@pytest.mark.parametrize('args, detached', [
    (['DRY_RUN=1'], False), (['MOTOR=A', 'dry_run=On'], False), (['--dry-run'], False),
    (['DRY_RUN=0'], True), (['MOTOR=A'], True), ([], True),
])
def test_save_runs_only_a_dry_run_in_the_foreground(tmp_path, args, detached):
    # a dry run's lines go straight to the console; a save restarts Klipper through
    # Moonraker, which waits while RUN_SHELL_COMMAND holds Klipper's queue: detached
    called = launched(tmp_path, 'save.sh', args)
    assert (called[0] != '--sync') is detached and called[called.index('save'):] == ['save'] + args


LAUNCHERS = sorted(name for name in os.listdir(REPO) if name.endswith('.sh')
                   and name not in ('run.sh', 'stop.sh', 'install.sh', 'uninstall.sh'))


@pytest.mark.parametrize('script', LAUNCHERS)
def test_a_launcher_hands_the_tool_the_macro_parameters_as_they_are(tmp_path, script):
    # a detached run has no terminal and never asks to proceed: it needs no --yes. Only
    # status, a plain analyze and a dry-run save run in the foreground: a run that moves
    # the printer or restarts Klipper hangs there: RUN_SHELL_COMMAND holds Klipper's queue
    command = script[:-len('.sh')].replace('_', '-')
    called = launched(tmp_path, script, ['MOTOR=A', 'DRY_RUN=1'])
    assert called[:called.index(command)] == (
        ['--sync'] if script in ('status.sh', 'analyze.sh', 'save.sh') else [])
    assert called[called.index(command):] == [command, 'MOTOR=A', 'DRY_RUN=1']


@pytest.mark.parametrize('script, first, keys', [('analyze.sh', '--apply|', ('APPLY', 'SAVE')),
                                                 ('save.sh', '--dry-run|', ('DRY_RUN',))])
def test_the_launchers_take_the_yes_values_the_cli_takes(script, first, keys):
    import re

    from chopper_autotune.cli import TRUE_VALUES
    with open(os.path.join(REPO, script)) as lines:
        pattern = next(line for line in lines if line.strip().startswith(first))
    patterns = re.findall(r'\b(%s)=(\w+)' % '|'.join(keys), pattern)
    assert sorted(patterns) == sorted((key, value) for key in keys for value in TRUE_VALUES)



@pytest.mark.parametrize('argv, error, reason, display', [
    (['analyze', 'MOTOR=C', 'APPLY=1'],
     "argument --motor/--axis: expected MOTOR=A or MOTOR=B, one motor per run; got 'C'",
     "fix MOTOR=: expected MOTOR=A or MOTOR=B, one motor per run; got 'C'", 'FAIL fix MOTOR=:'),
    (['demo', 'MOTOR=A', 'SPEED=0'],
     "argument --speed: expected one speed as a whole number of mm/s above 0, e.g. 40, got '0'",
     "fix SPEED=: expected one speed as a whole number of mm/s above 0, e.g. 40, got '0'",
     'FAIL fix SPEED=:'),
    (['tune', 'FOO=1'], 'unrecognized arguments: --foo 1',
     'drop FOO=1: CHOPPER_TUNE has no such parameter', 'FAIL drop FOO=1:'),
    (['tune', 'FOO=1', 'MOTOR=A', 'BAR=x'], 'unrecognized arguments: --foo 1 --bar x',
     'drop FOO=1 BAR=x: CHOPPER_TUNE has no such parameters', 'FAIL drop FOO=1 '),
    (['collect', 'SPEED=60', 'DRY_RUN=2'], None, "fix DRY_RUN=: expected 1 or 0, got '2'",
     'FAIL fix DRY_RUN')])
def test_a_bad_parameter_reaches_the_console_and_the_display_however_slow_the_start(
        argv, error, reason, display, monkeypatch, capsys):
    # run.sh prints a start-up failure only within its first second: past it the run is
    # detached, and the CLI itself says what failed, on a printer of each release, in the
    # words of the macro: its parameters, not the CLI's options. stderr keeps argparse's
    import types

    import klipper_front
    from chopper_autotune import cli, klippy
    from chopper_autotune.collect import failure_display
    from test_klipper_front import SOURCES, printer_cfg, require
    for source in SOURCES:
        require(source)
        front = klipper_front.Front(source, printer_cfg())
        monkeypatch.setattr(klippy, 'Klippy', lambda path: types.SimpleNamespace(
            connect=lambda: front.connect()))
        monkeypatch.setattr(klippy, 'find_socket', lambda explicit=None: '<front>')
        message = '%s FAILED: %s' % (argv[0], reason)
        for sync in (True, False):
            # a foreground run's macro holds Klipper's G-code queue: its output goes there
            if sync:
                monkeypatch.setenv('CHOPPER_SYNC', '1')
            else:
                monkeypatch.delenv('CHOPPER_SYNC', raising=False)
            with pytest.raises(SystemExit) as failed:
                cli.main(argv)
            assert failed.value.code == (2 if error else reason)
            assert ('error: %s\n' % error in capsys.readouterr().err) == bool(error)
            assert ('echo: ' + message in front.console) != sync
        assert front.status({'display_status': ['message']})['display_status']['message'] \
            == failure_display(message)
        assert failure_display(message)[:16] == display
        assert not [line for line in front.console if line.startswith('!!')]
