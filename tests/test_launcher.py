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

