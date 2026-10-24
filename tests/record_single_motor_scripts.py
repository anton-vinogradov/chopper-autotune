"""Records the single-motor references, tests/single_motor_scripts, from the code of main.
When and how to run it: see tests/test_single_motor_scripts.py.

    python tests/record_single_motor_scripts.py SOURCE    record from SOURCE
    python tests/record_single_motor_scripts.py --check   record from the code the references
                                                          came from, compare and change nothing

SOURCE is a ref or commit of this repository (origin/main), or the directory of a checkout,
which stands for the commit it has checked out. In a temporary tree the recording runs that
commit's chopper_autotune and tests/conftest.py, whose fixtures patch that code, with the
other tests of this tree. single_motor_scripts/recorded-from names the commit."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile

TESTS = os.path.dirname(os.path.abspath(__file__))
REFERENCE = os.path.join(TESTS, 'single_motor_scripts')
RECORDED_FROM = 'recorded-from'
OF_THE_CODE = ('chopper_autotune', 'tests/conftest.py')
# set for the pytest run in the temporary tree, which records
RECORD_TO = os.environ.get('CHOPPER_RECORD_TO')


def git(*args, binary: bool = False, cwd: str = None):
    run = subprocess.run(['git', *args], cwd=cwd or os.path.dirname(TESTS), stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE)
    if run.returncode:
        sys.exit('git %s: %s' % (' '.join(args), run.stderr.decode().strip()))
    return run.stdout if binary else run.stdout.decode().strip()


def code_of(source: str) -> str:
    """The commit of a ref, or of what a checkout's directory has checked out."""
    if os.path.isdir(source):
        if git('status', '--porcelain', '--', *OF_THE_CODE, cwd=source):
            sys.exit('%s has changes in %s that are not committed: a recording runs a commit.'
                     % (source, ' or '.join(OF_THE_CODE)))
        source = git('rev-parse', 'HEAD', cwd=source)
    return git('rev-parse', '--verify', source + '^{commit}')


def assemble(code: str, root: str):
    """The tree a recording runs in: these tests, with chopper_autotune and conftest.py of
    commit `code`."""
    shutil.copytree(TESTS, os.path.join(root, 'tests'),
                    ignore=shutil.ignore_patterns('.klipper-src', '__pycache__', '.pytest_cache'))
    subprocess.run(['tar', '-x', '-C', root], check=True,
                   input=git('archive', code, *OF_THE_CODE, binary=True))


def record(code: str, into: str):
    with tempfile.TemporaryDirectory() as root:
        assemble(code, root)
        # the tree has no Klipper sources of its own. Without the release the references
        # are printed on, each case fails instead of skipping: a skipped case records nothing,
        # and store() would delete its references
        env = dict(os.environ, CHOPPER_RECORD_TO=into, CHOPPER_CONTRACT='1',
                   KLIPPER_SRC_DIR=os.path.abspath(os.environ.get('KLIPPER_SRC_DIR')
                                                   or os.path.join(TESTS, '.klipper-src')))
        test = os.path.join('tests', os.path.basename(__file__))
        if subprocess.run([sys.executable, '-m', 'pytest', '-q', test], cwd=root, env=env).returncode:
            sys.exit('The recording from %s failed. The references did not change.' % code)


def recorded(folder: str) -> 'set[str]':
    return {name for name in os.listdir(folder) if name.endswith(('.txt', '.out'))}


def differences(recording: str, reference: str) -> 'list[str]':
    """A line for each file of the recording that differs from its reference byte for
    byte, and for each file that only one of the two has."""
    made, kept = recorded(recording), recorded(reference)
    lines = []
    for name in sorted(made | kept):
        if name not in kept:
            lines.append('new: ' + name)
        elif name not in made:
            lines.append('not recorded: ' + name)
        else:
            with open(os.path.join(recording, name), 'rb') as new, \
                    open(os.path.join(reference, name), 'rb') as old:
                if new.read() != old.read():
                    lines.append('changed: ' + name)
    return lines


def store(recording: str, reference: str, code: str):
    """The recording becomes the references, recorded-from names its code."""
    for name in recorded(reference) - recorded(recording):
        os.remove(os.path.join(reference, name))
    for name in recorded(recording):
        shutil.copyfile(os.path.join(recording, name), os.path.join(reference, name))
    with open(os.path.join(reference, RECORDED_FROM), 'w') as source:
        source.write(code + '\n')


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='Record the single-motor references from the code of main.')
    parser.add_argument('source', nargs='?',
                        help='a ref or commit (origin/main), or the directory of a checkout for the '
                             'commit it has checked out; with --check, by default the commit in '
                             'single_motor_scripts/recorded-from')
    parser.add_argument('--check', action='store_true',
                        help='compare a recording with the references byte for byte, change nothing')
    args = parser.parse_args(argv)
    if args.source:
        code = code_of(args.source)
    elif args.check:
        with open(os.path.join(REFERENCE, RECORDED_FROM)) as source:
            code = source.read().strip()
    else:
        parser.error('name the code of main to record from, for example origin/main')
    with tempfile.TemporaryDirectory() as recording:
        record(code, recording)
        lines = differences(recording, REFERENCE)
        if args.check:
            print('The recording from %s differs from the references in %d files:\n%s'
                  % (code, len(lines), '\n'.join(lines)) if lines else
                  'The recording from %s equals the references byte for byte.' % code)
            return 1 if lines else 0
        store(recording, REFERENCE, code)
    print('Recorded from %s: %d files of the references changed:\n%s'
          % (code, len(lines), '\n'.join(lines)) if lines else
          'Recorded from %s: the references did not change.' % code)
    return 0


if RECORD_TO:
    import pytest

    import chopper_autotune
    from test_klipper_front import SOURCES, require
    from test_single_motor_scripts import CASES, PRINTED_ON, run

    @pytest.mark.parametrize('case, printer', CASES)
    def test_record(case, printer, tmp_path, monkeypatch, capsys):
        # an installed chopper_autotune would record the code it points to
        assert os.path.dirname(os.path.realpath(chopper_autotune.__file__)) == os.path.realpath(
            os.path.join(TESTS, '..', 'chopper_autotune'))
        require(PRINTED_ON if PRINTED_ON in SOURCES else None)
        scripts, printed = run(PRINTED_ON, printer, case, tmp_path, monkeypatch, capsys)
        for kind, text in (('txt', scripts), ('out', printed)):
            with open(os.path.join(RECORD_TO, '%s-%s.%s' % (case, printer, kind)), 'w') as out:
                out.write(text)


if __name__ == '__main__':
    sys.exit(main())
