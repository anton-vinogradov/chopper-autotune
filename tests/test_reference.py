"""What a user reads about the commands matches them: every parameter of a macro is in its
section of README.md and README_RU.md, CHOPPER_STOP reads the same wherever it is described,
CHOPPER_STOP stops every command that moves the printer, and the start-up self-check knows
every macro."""
import argparse
import os
import re

import pytest

from chopper_autotune.cli import build_parser
from klipper_config import CFG, our_macros, read_like_klipper

REPO = os.path.dirname(CFG)
READMES = ('README.md', 'README_RU.md')
# for scripts and experts: the README names them once, outside the command sections
EXPERT = {'SOCKET', 'URL', 'TRIM', 'NO_RAW', 'CSV', 'YES'}


def read(name: str) -> str:
    with open(os.path.join(REPO, name)) as source:
        return source.read()


def sections(readme: str) -> 'dict[str, str]':
    """The paragraphs of each command in the command reference, from its **CHOPPER_X** to
    the next command's."""
    start = re.search(r'^### (Command reference|Справочник команд)$', readme, re.M).end()
    found, name = {}, None
    for paragraph in readme[start:readme.index('\n## ', start)].split('\n\n'):
        heading = re.match(r'\*\*(CHOPPER_[A-Z_]+)', paragraph)
        name = heading.group(1) if heading else name
        if name:
            found[name] = found.get(name, '') + paragraph + '\n\n'
    return found


def commands() -> 'dict[str, argparse.ArgumentParser]':
    return next(action for action in build_parser()._actions
                if isinstance(action, argparse._SubParsersAction)).choices


def macro(command: str) -> str:
    return 'CHOPPER_' + command.upper().replace('-', '_')


def parameters(command: str) -> 'list[str]':
    """The macro parameters of a command: MEASURE_TIME for --measure-time."""
    return [flags[0][2:].replace('-', '_').upper() for flags in
            ([flag for flag in action.option_strings if flag.startswith('--')]
             for action in commands()[command]._actions
             if not isinstance(action, argparse._HelpAction)) if flags]


MACRO_COMMANDS = [command for command in commands() if macro(command) in our_macros()]


@pytest.mark.parametrize('readme', READMES)
def test_every_macro_has_a_section_in_the_command_reference(readme):
    assert sorted(sections(read(readme))) == sorted(our_macros())


@pytest.mark.parametrize('readme', READMES)
@pytest.mark.parametrize('command', MACRO_COMMANDS)
def test_every_parameter_of_a_macro_is_in_its_section(readme, command):
    section = sections(read(readme))[macro(command)]
    assert [name for name in parameters(command) if name not in EXPERT
            and not re.search(r'`%s[`=]' % name, section)] == []


def test_chopper_stop_reads_the_same_wherever_it_is_described():
    text = read_like_klipper(CFG).get('gcode_macro CHOPPER_STOP', 'description')
    # each place in full, to its end: a text cut short is a prefix of the whole one
    comment = read('stop.sh').split('\n#\n')[0]
    assert ' '.join(line[2:] for line in comment.splitlines() if line.startswith('# ')) == text
    # the plan and the touchscreen panel link to the command reference
    reference = sections(read('README.md'))['CHOPPER_STOP'].replace('`', '')
    assert reference.split(' — ', 1)[1] == '%s%s\n\n' % (text[0].lower(), text[1:])


def test_chopper_stop_stops_every_command_that_moves_the_printer():
    # such a command drives the printer over the klippy socket: SOCKET= is its parameter
    stopped = re.search(r'pkill -TERM -f "\[c\]hopper-autotune \(([^)]*)\)"', read('stop.sh')).group(1)
    assert sorted(stopped.split('|')) == sorted(command for command in commands()
                                                if 'SOCKET' in parameters(command))


def test_the_self_check_knows_every_macro():
    check = read_like_klipper(CFG).get('delayed_gcode chopper_autotune_selfcheck', 'gcode')
    names = re.search(r'for name in \[([^\]]*)\]', check).group(1)
    assert sorted(re.findall(r"'([A-Z_]+)'", names)) == sorted(
        name[len('CHOPPER_'):] for name in our_macros())
