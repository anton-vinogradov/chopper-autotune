"""Klipper's view of chopper_autotune.cfg: printer.configfile.settings as Klipper builds
it from a read config, and the start-up self-check rendered the way Klipper renders a
macro (jinja2 with '{' '}' expression delimiters)."""
import configparser
import os
import re

import pytest

CFG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'chopper_autotune.cfg')


def read_like_klipper(*paths) -> configparser.RawConfigParser:
    """Every file read on its own, like Klipper reads each include; '#' starts a comment
    anywhere on a line, as Klipper strips it before parsing."""
    fileconfig = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(';', '#'))
    for path in paths:
        with open(path) as source:
            fileconfig.read_string('\n'.join(line.split('#', 1)[0] for line in source.read().split('\n')))
    return fileconfig


def settings_of(fileconfig) -> dict:
    """Section names lower-cased, as Klipper's access tracking records them."""
    return {section.lower(): {option: fileconfig.get(section, option)
                              for option in fileconfig.options(section)}
            for section in fileconfig.sections()}


def our_macros() -> 'list[str]':
    return [section.split()[1] for section in read_like_klipper(CFG).sections()
            if section.startswith('gcode_macro CHOPPER_')]


def named(error: str) -> 'list[str]':
    """The macro names in the self-check's error."""
    return re.split(' runs? ', error.split('chopper-autotune: ', 1)[1], maxsplit=1)[0].split(', ')


class RaisedError(Exception):
    pass


def run_selfcheck(settings: dict, display_status: bool = True) -> 'tuple[str | None, str | None]':
    """What the start-up self-check leaves: (the display message, the console error).
    Runs the script _CHOPPER_SELFCHECK renders line by line, as Klipper does; M117
    exists only with [display_status] loaded (Mainsail's and Fluidd's configs have it)."""
    try:
        import jinja2
    except ImportError:
        if os.environ.get('CHOPPER_CONTRACT'):
            pytest.fail('jinja2 is missing: pip install jinja2')
        pytest.skip('jinja2 is missing')

    def raise_error(message):
        raise RaisedError(message)

    def render(macro, params=None):
        script = settings['gcode_macro ' + macro]['gcode']
        printer = dict({'configfile': {'settings': settings}},
                       **({'display_status': {'message': None}} if display_status else {}))
        return jinja2.Environment('{%', '%}', '{', '}').from_string(script).render(
            {'printer': printer, 'params': params or {},
             'action_raise_error': raise_error, 'action_respond_info': lambda message: ''})

    display = None
    for line in (line.strip() for line in render('_chopper_selfcheck').split('\n')):
        if line.startswith('M117 '):
            assert display_status, 'M117 without [display_status] is an unknown command'
            display = line[len('M117 '):]
        elif line.startswith('_CHOPPER_REPLACED '):
            params = dict(word.split('=', 1) for word in line.split()[1:])
            try:
                render('_chopper_replaced', params)
            except RaisedError as raised:
                return display, str(raised)
        else:
            assert not line, 'unexpected G-code from the self-check: %r' % line
    return display, None


def selfcheck(settings: dict) -> 'str | None':
    """The console error of the start-up self-check, or None."""
    return run_selfcheck(settings)[1]
