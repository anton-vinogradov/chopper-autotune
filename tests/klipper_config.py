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


def selfcheck(settings: dict) -> 'str | None':
    """The error _CHOPPER_SELFCHECK raises, or None."""
    try:
        import jinja2
    except ImportError:
        if os.environ.get('CHOPPER_CONTRACT'):
            pytest.fail('jinja2 is missing: pip install jinja2')
        pytest.skip('jinja2 is missing')

    def raise_error(message):
        raise RaisedError(message)

    script = settings['gcode_macro _chopper_selfcheck']['gcode']
    template = jinja2.Environment('{%', '%}', '{', '}').from_string(script)
    try:
        template.render({'printer': {'configfile': {'settings': settings}},
                         'action_raise_error': raise_error,
                         'action_respond_info': lambda message: ''})
    except RaisedError as raised:
        return str(raised)
    return None
