"""Klipper merges a section that several config files declare and keeps, per option,
the value it read last: another tool's [gcode_macro CHOPPER_TUNE] read after ours
replaces our macro without a word, and CHOPPER_TUNE runs that tool (#132). The start-up
self-check names such macros; the KlipperScreen panel marks their buttons and sends
nothing to them."""
import builtins
import importlib.util
import os
import sys
import types
from types import SimpleNamespace

import pytest

from klipper_config import CFG, named, our_macros, read_like_klipper, run_selfcheck, selfcheck, settings_of

PANEL = os.path.join(os.path.dirname(CFG), 'klipperscreen', 'chopper.py')


def another_tool(tmp_path, name='CHOPPER_TUNE'):
    path = tmp_path / ('%s.cfg' % name.lower())
    path.write_text('[gcode_macro %s]\ndescription: another tuner\ngcode:\n    _chop_workflow\n' % name)
    return str(path)


def gschpoozi(tmp_path):
    """Its CHOPPER_ANALYZE calls a shell command of our name, which runs its own script."""
    path = tmp_path / 'chopper-tuning.cfg'
    path.write_text('[gcode_shell_command chopper_analyze]\n'
                    'command: python3 ~/gschpoozi/scripts/tools/chopper_analyze.py\n'
                    '[gcode_macro CHOPPER_ANALYZE]\n'
                    'gcode:\n    RUN_SHELL_COMMAND CMD=chopper_analyze PARAMS="{rawparams}"\n')
    return str(path)


def wrapper(tmp_path):
    """A macro of your own around ours: it still runs our tool."""
    path = tmp_path / 'my_macros.cfg'
    path.write_text('[gcode_macro CHOPPER_TUNE]\ngcode:\n    G28\n'
                    "    RUN_SHELL_COMMAND CMD=chopper_tune PARAMS='{rawparams}'\n")
    return str(path)


def test_our_file_alone_passes_the_self_check():
    assert run_selfcheck(settings_of(read_like_klipper(CFG))) == (None, None)


@pytest.mark.parametrize('name', our_macros())
def test_a_macro_replaced_by_a_file_read_later_is_named(tmp_path, name):
    display, error = run_selfcheck(settings_of(read_like_klipper(CFG, another_tool(tmp_path, name))))
    assert error.startswith('chopper-autotune: %s runs another tool.' % name)
    assert named(error) == [name]
    assert "Keep one of the two tools: see 'Macro name conflicts'" in error
    # the display keeps it when a frontend still starting up misses the console line
    assert display == 'chopper-autotune: %s runs another tool. README: Macro name conflicts' % name


def test_every_replaced_macro_is_named_in_one_error(tmp_path):
    error = selfcheck(settings_of(read_like_klipper(
        CFG, another_tool(tmp_path, 'CHOPPER_TUNE'), another_tool(tmp_path, 'CHOPPER_ANALYZE'))))
    assert error.startswith('chopper-autotune: CHOPPER_TUNE, CHOPPER_ANALYZE run another tool.')


def test_without_a_display_status_the_console_error_still_comes(tmp_path):
    # M117 is an unknown command there, and it would abort the check before the error
    settings = settings_of(read_like_klipper(CFG, another_tool(tmp_path)))
    display, error = run_selfcheck(settings, display_status=False)
    assert display is None and named(error) == ['CHOPPER_TUNE']


def test_a_shell_command_of_our_name_running_another_script_is_named(tmp_path):
    # gschpoozi's CHOPPER_ANALYZE passes the macro check: CMD=chopper_analyze
    assert named(selfcheck(settings_of(read_like_klipper(CFG, gschpoozi(tmp_path))))) == ['CHOPPER_ANALYZE']


def test_a_wrapper_of_your_own_that_runs_our_tool_is_not_named(tmp_path):
    assert run_selfcheck(settings_of(read_like_klipper(CFG, wrapper(tmp_path)))) == (None, None)


def test_a_tool_read_before_ours_loses_quietly(tmp_path):
    # ours is read last and wins: the other tool's CHOPPER_TUNE runs ours
    fileconfig = read_like_klipper(another_tool(tmp_path), CFG)
    assert 'CMD=chopper_tune' in fileconfig.get('gcode_macro CHOPPER_TUNE', 'gcode')
    assert selfcheck(settings_of(fileconfig)) is None


def test_every_macro_calls_the_shell_command_of_its_own_name():
    # the rule the self-check and the panel recognise our macros by
    fileconfig = read_like_klipper(CFG)
    assert len(our_macros()) == 14
    for name in our_macros():
        assert 'RUN_SHELL_COMMAND CMD=%s' % name.lower() in fileconfig.get('gcode_macro ' + name, 'gcode')
        assert 'chopper-autotune/' in fileconfig.get('gcode_shell_command ' + name.lower(), 'command')


def test_the_self_check_runs_at_every_start():
    fileconfig = read_like_klipper(CFG)
    section = 'delayed_gcode chopper_autotune_selfcheck'
    assert float(fileconfig.get(section, 'initial_duration')) > 0     # 0 never runs
    assert fileconfig.get(section, 'gcode').strip() == '_CHOPPER_SELFCHECK'


class Label:
    def __init__(self):
        self.text = None

    def set_text(self, text):
        self.text = text


@pytest.fixture
def panel_module(monkeypatch):
    """klipperscreen/chopper.py without GTK or KlipperScreen: only its logic runs."""
    gi = types.ModuleType('gi')
    gi.require_version = lambda *args: None
    repository = types.ModuleType('gi.repository')
    repository.GLib = SimpleNamespace(markup_escape_text=lambda text: text)
    repository.Gtk = SimpleNamespace(Label=Label)
    repository.Pango = SimpleNamespace()
    gi.repository = repository
    screen_panel = types.ModuleType('ks_includes.screen_panel')
    screen_panel.ScreenPanel = object
    ks_includes = types.ModuleType('ks_includes')
    ks_includes.screen_panel = screen_panel
    for module in (gi, repository, ks_includes, screen_panel):
        monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setattr(builtins, '_', lambda text: text, raising=False)
    spec = importlib.util.spec_from_file_location('chopper_panel', PANEL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def panel_on(module, *paths):
    """A panel on a printer whose config Klipper read from these files."""
    fileconfig = read_like_klipper(*paths) if paths else None
    config = {section: dict(fileconfig.items(section)) for section in fileconfig.sections()} if paths else {}
    panel = module.Panel.__new__(module.Panel)
    panel.sent, panel.shown = [], []
    panel._screen = SimpleNamespace(
        printer=SimpleNamespace(get_config_section=lambda section: config.get(section, False)),
        _confirm_send_action=lambda widget, confirm, method, params: panel.sent.append(params['script']),
        _ws=SimpleNamespace(klippy=SimpleNamespace(gcode_script=panel.sent.append)))
    panel.status = SimpleNamespace(set_markup=panel.shown.append)
    return panel


def test_the_panel_sends_nothing_to_a_replaced_macro(tmp_path, panel_module):
    panel = panel_on(panel_module, CFG, another_tool(tmp_path))
    panel.run(None, 'CHOPPER_TUNE MOTOR=AB SAVE=1', 'Tune?')
    assert panel.sent == []
    assert 'CHOPPER_TUNE here runs another tool' in panel.shown[-1]


def test_the_panel_sends_nothing_to_our_name_running_another_script(tmp_path, panel_module):
    panel = panel_on(panel_module, CFG, gschpoozi(tmp_path))
    panel.run(None, 'CHOPPER_ANALYZE', 'Analyze?')
    assert panel.sent == []


def test_the_panel_sends_a_wrapper_of_your_own(tmp_path, panel_module):
    panel = panel_on(panel_module, CFG, wrapper(tmp_path))
    panel.run(None, 'CHOPPER_TUNE MOTOR=AB SAVE=1', 'Tune?')
    assert panel.sent == ['CHOPPER_TUNE MOTOR=AB SAVE=1']


def test_the_panel_checks_stop_too(tmp_path, panel_module):
    panel = panel_on(panel_module, CFG, another_tool(tmp_path, 'CHOPPER_STOP'))
    panel.stop(None)
    assert panel.sent == []


@pytest.mark.parametrize('paths', [(CFG,), ()])
def test_the_panel_sends_our_macros_and_leaves_an_unknown_one_to_klipper(panel_module, paths):
    # no config yet (Klipper not connected): Klipper itself answers an unknown command
    panel = panel_on(panel_module, *paths)
    panel.run(None, 'CHOPPER_TUNE MOTOR=AB SAVE=1', 'Tune?')
    assert panel.sent == ['CHOPPER_TUNE MOTOR=AB SAVE=1']


def test_the_panel_marks_a_replaced_button(tmp_path, panel_module):
    panel = panel_on(panel_module, CFG, another_tool(tmp_path), another_tool(tmp_path, 'CHOPPER_STOP'))
    panel.buttons = {'1 Belts': Label(), '2,4 Tune': Label(), 'Map': Label(), 'Stop': Label()}
    panel.commands = {'1 Belts': 'CHOPPER_BELTS', '2,4 Tune': 'CHOPPER_TUNE MOTOR=AB SAVE=1',
                      'Map': 'CHOPPER_MAP', 'Stop': 'CHOPPER_STOP'}
    panel.step_states = lambda: {'1 Belts': True, '2,4 Tune': True}
    panel.mark_done_steps()
    assert {label: button.text for label, button in panel.buttons.items()} == {
        '1 Belts': '✓ 1 Belts', '2,4 Tune': '⚠ 2,4 Tune', 'Map': 'Map', 'Stop': '⚠ Stop'}


def test_the_panel_does_not_show_autotunes_motor_as_tuned(tmp_path, panel_module):
    # klipper_tmc_autotune writes its own chopper over these lines at every start
    managed = tmp_path / 'printer.cfg'
    managed.write_text('[tmc2209 stepper_x]\ndriver_tbl: 0\ndriver_toff: 8\ndriver_hstrt: 7\n'
                       'driver_hend: 5\n[autotune_tmc stepper_x]\nmotor: ldo-42sth48-2004mah\n'
                       '[tmc2209 stepper_y]\ndriver_tbl: 0\ndriver_toff: 8\ndriver_hstrt: 7\n'
                       'driver_hend: 5\n')
    panel = panel_on(panel_module, str(managed))
    assert panel.autotune('stepper_x') and not panel.autotune('stepper_y')
    assert panel.tuned_registers('stepper_x') == ''
    assert panel.tuned_registers('stepper_y') == '0/8/7/5'
