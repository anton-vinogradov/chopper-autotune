"""The single-motor recorder (record_single_motor_scripts.py) without a recording. A
recording runs the commit it is given, with that commit's conftest.py, not this tree's code:
a recorder that took this tree's code would turn any change the branch makes for a single
motor into a reference. A checkout stands for the commit it has checked out, and
recorded-from names that commit, not a folder of one machine. The check records from that
commit, names each file the recording does not reproduce and then fails; a recording
replaces the references."""
import os
import subprocess

import pytest

import record_single_motor_scripts as recorder


def files(folder) -> dict:
    return {name: (folder / name).read_text() for name in os.listdir(folder)}


def main_repository(path, monkeypatch, *versions) -> str:
    """A repository of main whose commits give chopper_autotune/__init__.py and
    tests/conftest.py each of these texts in turn; the recorder's git works in it."""
    for folder in ('chopper_autotune', 'tests'):
        (path / folder).mkdir(parents=True)
    # a GIT_DIR from the caller (a git hook sets one) would take these commits
    for name in [name for name in os.environ if name.startswith('GIT_')]:
        monkeypatch.delenv(name)
    for name in ('AUTHOR', 'COMMITTER'):
        monkeypatch.setenv('GIT_%s_NAME' % name, 'main')
        monkeypatch.setenv('GIT_%s_EMAIL' % name, 'main@localhost')
    subprocess.run(['git', 'init', '-q', str(path)], check=True)
    for text in versions:
        for name in ('chopper_autotune/__init__.py', 'tests/conftest.py'):
            (path / name).write_text(text)
        subprocess.run(['git', '-C', str(path), 'add', '-A'], check=True)
        subprocess.run(['git', '-C', str(path), '-c', 'commit.gpgsign=false', '-c',
                        'core.hooksPath=/dev/null', 'commit', '-qm', text], check=True)
    monkeypatch.setenv('GIT_DIR', str(path / '.git'))
    return str(path)


def commit(main: str, ref: str) -> str:
    return subprocess.run(['git', '-C', main, 'rev-parse', ref], check=True, stdout=subprocess.PIPE,
                          text=True).stdout.strip()


def reference_of(tmp_path, monkeypatch):
    """The references the recorder keeps, made here: a map case, recorded from 0123abc."""
    reference = tmp_path / 'reference'
    reference.mkdir()
    (reference / 'map-voron.out').write_text('G28\n')
    (reference / 'recorded-from').write_text('0123abc\n')
    monkeypatch.setattr(recorder, 'REFERENCE', str(reference))
    return reference


def recording(monkeypatch, printed: str) -> 'list[str]':
    """A recording that prints `printed` for the map case; the commits it was asked for."""
    codes = []

    def record(code, into):
        codes.append(code)
        with open(os.path.join(into, 'map-voron.out'), 'w') as out:
            out.write(printed)
    monkeypatch.setattr(recorder, 'record', record)
    return codes


@pytest.mark.parametrize('source', ['directory', 'commit'])
def test_a_recording_runs_the_code_it_is_given_with_these_tests(tmp_path, monkeypatch, source):
    main = main_repository(tmp_path / 'main', monkeypatch, '"""older"""\n', '"""main"""\n')
    root = tmp_path / 'root'
    root.mkdir()
    if source == 'directory':
        code, text = recorder.code_of(main), '"""main"""\n'
        assert code == commit(main, 'HEAD')
    else:
        code, text = recorder.code_of('HEAD~1'), '"""older"""\n'
    recorder.assemble(code, str(root))
    assert files(root / 'chopper_autotune') == {'__init__.py': text}
    # its fixtures patch that code: one patching a name the commit lacks fails each case
    assert (root / 'tests' / 'conftest.py').read_text() == text
    with open(os.path.join(recorder.TESTS, 'test_single_motor_scripts.py')) as ours:
        assert (root / 'tests' / 'test_single_motor_scripts.py').read_text() == ours.read()
    assert sorted(os.listdir(root / 'tests' / 'single_motor_scripts')) == sorted(
        os.listdir(recorder.REFERENCE))


@pytest.mark.parametrize('changed', ['chopper_autotune/__init__.py', 'tests/conftest.py'])
def test_a_checkout_with_changes_that_are_not_committed_is_refused(tmp_path, monkeypatch, changed):
    main = main_repository(tmp_path / 'main', monkeypatch, '"""main"""\n')
    (tmp_path / 'main' / changed).write_text('"""changed"""\n')
    with pytest.raises(SystemExit, match='has changes in chopper_autotune or tests/conftest.py '
                                         'that are not committed'):
        recorder.code_of(main)


@pytest.mark.parametrize('printed, verdict, status', [
    ('G28\n', 'The recording from 0123abc equals the references byte for byte.', 0),
    ('G28 \n', 'The recording from 0123abc differs from the references in 1 files:\n'
               'changed: map-voron.out', 1),
])
def test_the_check_records_from_the_commit_the_references_came_from(tmp_path, monkeypatch, capsys,
                                                                    printed, verdict, status):
    reference = reference_of(tmp_path, monkeypatch)
    codes = recording(monkeypatch, printed)
    assert recorder.main(['--check']) == status
    assert codes == ['0123abc']
    assert capsys.readouterr().out == verdict + '\n'
    assert files(reference) == {'map-voron.out': 'G28\n', 'recorded-from': '0123abc\n'}


def test_a_recording_from_a_checkout_replaces_the_references_and_names_its_commit(
        tmp_path, monkeypatch, capsys):
    main = main_repository(tmp_path / 'main', monkeypatch, '"""main"""\n')
    reference = reference_of(tmp_path, monkeypatch)
    codes = recording(monkeypatch, 'G28 \n')
    head = commit(main, 'HEAD')
    assert recorder.main([main]) == 0
    assert codes == [head]
    assert files(reference) == {'map-voron.out': 'G28 \n', 'recorded-from': head + '\n'}
    assert capsys.readouterr().out == (
        'Recorded from %s: 1 files of the references changed:\nchanged: map-voron.out\n' % head)


def test_the_check_names_each_file_a_recording_does_not_reproduce(tmp_path):
    recording, reference = tmp_path / 'recording', tmp_path / 'reference'
    recording.mkdir()
    reference.mkdir()
    for name in ('same.txt', 'changed.out', 'gone.txt', 'printer-voron.cfg'):
        (reference / name).write_text('G28\n')
    for name in ('same.txt', 'new.out'):
        (recording / name).write_text('G28\n')
    (recording / 'changed.out').write_text('G28 \n')
    assert recorder.differences(str(recording), str(reference)) == [
        'changed: changed.out', 'not recorded: gone.txt', 'new: new.out']


def test_a_recording_replaces_the_references_and_names_its_code(tmp_path):
    recording, reference = tmp_path / 'recording', tmp_path / 'reference'
    recording.mkdir()
    reference.mkdir()
    for name, text in (('gone.txt', 'G28\n'), ('changed.out', 'G28\n'), ('printer-voron.cfg', '[printer]\n')):
        (reference / name).write_text(text)
    for name, text in (('changed.out', 'G28 \n'), ('new.txt', 'M84\n')):
        (recording / name).write_text(text)
    recorder.store(str(recording), str(reference), '0123abc')
    assert files(reference) == {'changed.out': 'G28 \n', 'new.txt': 'M84\n',
                                'printer-voron.cfg': '[printer]\n', 'recorded-from': '0123abc\n'}
