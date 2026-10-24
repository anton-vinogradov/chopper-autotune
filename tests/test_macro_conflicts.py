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

import klipper_front
from chopper_autotune import belts, current, demo, envelope, extruder, resonance_map, tune
from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser, dispatch
from klipper_config import CFG, named, our_macros, read_like_klipper, run_selfcheck, selfcheck, settings_of
from test_klipper_front import SOURCES, awd_cfg, require
from test_rail import dual_y

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
    # the display keeps it when a frontend still starting up misses the console line, in
    # what a 16-character LCD row shows
    assert display == 'Clash %s' % name[len('CHOPPER_'):] and len(display) <= 16


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
    # the check itself, not a macro it calls: the console got a nested macro's error twice
    assert '_CHOPPER_REPLACED NAMES=' in fileconfig.get(section, 'gcode')


class Widget:
    """A GTK widget the panel builds: its children, and what a tap on it calls."""

    def __init__(self, *args, **kwargs):
        self.children, self.tap = [], None

    def add(self, child, *place):
        self.children.append(child)

    attach = pack_start = add

    def get_children(self):
        return self.children

    def connect(self, signal, handler, *data):
        if signal == 'clicked':
            self.tap = lambda: handler(self, *data)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None         # show, hide, set_policy and the like


class Label(Widget):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.text = self.markup = None

    def set_text(self, text):
        self.text = text

    def set_markup(self, markup):
        self.markup = markup


class ScreenPanel:
    """KlipperScreen's base of a panel: what ours takes from it."""

    def __init__(self, screen, title):
        self._screen, self._printer, self._gtk = screen, screen.printer, screen.gtk
        self.content = Widget()


@pytest.fixture
def panel_module(monkeypatch):
    """klipperscreen/chopper.py without GTK or KlipperScreen: only its logic runs."""
    gi = types.ModuleType('gi')
    gi.require_version = lambda *args: None
    repository = types.ModuleType('gi.repository')
    repository.GLib = SimpleNamespace(markup_escape_text=lambda text: text)
    repository.Gtk = SimpleNamespace(
        Label=Label, Grid=Widget, Box=Widget, EventBox=Widget, ScrolledWindow=Widget,
        Orientation=SimpleNamespace(VERTICAL=None), Align=SimpleNamespace(CENTER=None, START=None),
        PolicyType=SimpleNamespace(NEVER=None, AUTOMATIC=None))
    repository.Pango = SimpleNamespace(WrapMode=SimpleNamespace(WORD_CHAR=None))
    gi.repository = repository
    screen_panel = types.ModuleType('ks_includes.screen_panel')
    screen_panel.ScreenPanel = ScreenPanel
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
        _send_action=lambda widget, method, params: panel.sent.append(params['script']))
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


def test_the_panel_sends_stop(panel_module):
    # KlipperScreen renamed _ws.klippy to _ws.api (4afd4bef09): _send_action is in both
    panel = panel_on(panel_module, CFG)
    panel.show_status = panel.shown.append
    panel.stop(None)
    assert panel.sent == ['CHOPPER_STOP'] and 'Stopping' in panel.shown[-1]


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


@pytest.mark.parametrize('untested, prefix', [(['A'], 'tested: '), (None, 'set: ')])
def test_the_results_say_tested_where_the_envelope_ran_short(tmp_path, panel_module, monkeypatch,
                                                            untested, prefix):
    # no skip and a ladder the test stopped short of: the velocity is tested, not a limit
    import json
    recommend = {'max_velocity': 176, 'max_accel': 3000, 'print_accel': 2500}
    if untested:
        recommend['untested'] = untested
    (tmp_path / 'envelope.json').write_text(json.dumps(
        {'A': {'speed': '250+', 'accel': '12k+'}, 'recommend': recommend}))
    for name in ('STATE', 'CURRENT_STATE', 'BELTS_STATE', 'MAP_STATE'):
        monkeypatch.setattr(panel_module, name, str(tmp_path / 'none.json'))
    monkeypatch.setattr(panel_module, 'ENVELOPE_STATE', str(tmp_path / 'envelope.json'))
    panel = panel_on(panel_module, CFG)
    panel.motors = []
    assert prefix + '[printer] velocity ≤176' in panel.results_text()



def test_the_results_name_each_axis_on_limited_cartesian(tmp_path, panel_module, monkeypatch):
    # the head limits alone: Kalico refused a max_x_velocity left above them
    import json
    recommend = {'max_velocity': 471, 'max_accel': 11100, 'print_accel': None,
                 'per_axis': {'max_x_velocity': 400, 'max_y_velocity': 250,
                              'max_x_accel': 10000, 'max_y_accel': 5000}}
    (tmp_path / 'envelope.json').write_text(json.dumps(
        {'A': {'speed': '400', 'accel': '13k'}, 'recommend': recommend}))
    for name in ('STATE', 'CURRENT_STATE', 'BELTS_STATE', 'MAP_STATE'):
        monkeypatch.setattr(panel_module, name, str(tmp_path / 'none.json'))
    monkeypatch.setattr(panel_module, 'ENVELOPE_STATE', str(tmp_path / 'envelope.json'))
    panel = panel_on(panel_module, CFG)
    panel.motors = []
    assert ('[printer] velocity ≤471 (X ≤400, Y ≤250) · accel ≤11100 (X ≤10000, Y ≤5000)'
            in panel.results_text())


@pytest.fixture
def built_panel(panel_module, tmp_path, monkeypatch):
    """The panel KlipperScreen opens on a printer with this printer.cfg and our macros,
    before anything is measured. Its `sent` keeps each script a tap sends, with the
    question asked first."""
    for name in ('STATE', 'BELTS_STATE', 'ENVELOPE_STATE', 'MAP_STATE', 'CURRENT_STATE'):
        monkeypatch.setattr(panel_module, name, str(tmp_path / 'none.json'))

    def button(icon, label, style):
        widget, text = Widget(), Label()
        text.set_text(label)
        widget.add(text)
        return widget

    def build(cfg):
        printer = tmp_path / 'printer.cfg'
        printer.write_text(cfg)
        fileconfig = read_like_klipper(str(printer), CFG)
        config = {section: dict(fileconfig.items(section)) for section in fileconfig.sections()}
        sent = []
        screen = SimpleNamespace(
            printer=SimpleNamespace(get_config_section=lambda section: config.get(section, False),
                                    get_stat=lambda *args: None),
            gtk=SimpleNamespace(Button=button),
            _confirm_send_action=lambda widget, confirm, method, params:
                sent.append((params['script'], confirm)))
        panel = panel_module.Panel(screen, None)
        panel.sent = sent
        return panel
    return build


def buttons_of(panel):
    """Each button of the panel by the text on it."""
    return {button.children[0].text: button for button in panel.grid.children}


ENDSTOP = ('%s needs a physical endstop — this machine homes sensorless (StallGuard), and the '
           'skip referee cannot creep into a virtual switch. The plan here is shorter: tune once '
           '(no current step to adapt to), then 5 Extruder.')
RAIL = '%s does not work yet on an axis with two motors (chopper-autotune issue #129 on GitHub). '
CURRENT_ON_A_RAIL = RAIL % '3 Current' + 'The plan here is shorter: tune once, then 5 Extruder.'
MAP_ON_A_RAIL = RAIL % 'Map' + 'It maps motor A (stepper_x), and stepper_x1 here drives the same axis.'
COMMANDS = {'2,4 Tune': 'CHOPPER_TUNE MOTOR=AB SAVE=1', '3 Current': 'CHOPPER_CURRENT SAVE=1',
            'Envelope': 'CHOPPER_ENVELOPE', 'Map': 'CHOPPER_MAP'}


@pytest.mark.parametrize('cfg, refused', [
    (awd_cfg('voron-2209'), {'3 Current': CURRENT_ON_A_RAIL, 'Map': MAP_ON_A_RAIL}),
    # Map runs motor A, alone on X
    (dual_y('voron-2209'), {'3 Current': CURRENT_ON_A_RAIL}),
    # homing sensorless as well: Current and Envelope have no endstop for their referee
    (awd_cfg('meijjaa'), {'3 Current': ENDSTOP % '3 Current', 'Envelope': ENDSTOP % 'Envelope',
                          'Map': MAP_ON_A_RAIL}),
], ids=['awd', 'dual-y', 'awd-sensorless'])
def test_a_two_motor_axis_marks_the_buttons_that_refuse_it_and_a_tap_says_why(built_panel, cfg,
                                                                            refused):
    panel = built_panel(cfg)
    buttons = buttons_of(panel)
    assert {text[:-len(' ✗')] for text in buttons if text.endswith(' ✗')} == set(refused)
    for label, reason in refused.items():
        buttons[label + ' ✗'].tap()
        assert panel.status.markup == "<span size='large'>%s</span>" % reason
    assert panel.sent == []
    tapped = {'Envelope', 'Map'} - set(refused)
    for label in tapped:
        buttons[label].tap()
    assert {script for script, confirm in panel.sent} == {COMMANDS[label] for label in tapped}


@pytest.mark.parametrize('cfg', [awd_cfg('voron-2209'), dual_y('voron-2209'), awd_cfg('meijjaa')],
                         ids=['awd', 'dual-y', 'awd-sensorless'])
def test_a_tune_on_a_two_motor_axis_runs_once_then_goes_to_the_extruder(built_panel, cfg):
    panel = built_panel(cfg)
    buttons_of(panel)['2,4 Tune'].tap()
    assert panel.sent == [(
        'CHOPPER_TUNE MOTOR=AB SAVE=1',
        "Step 2 — tune both gantry motors' choppers at their resonances (~20 minutes of movement), "
        "SAVE the winners and restart Klipper. Run it once. Step 4 re-tunes for a new current from "
        "3 Current, which does not work yet on an axis with two motors. Then continue with 5 "
        "Extruder. Restore undoes.")]


@pytest.mark.parametrize('printer, refused', [('voron-2209', set()), ('meijjaa', {'3 Current', 'Envelope'})])
def test_one_motor_per_axis_gets_the_panel_it_had(built_panel, printer, refused):
    panel = built_panel(awd_cfg(printer, twins=False))
    buttons = buttons_of(panel)
    assert {text[:-len(' ✗')] for text in buttons if text.endswith(' ✗')} == refused
    for label in refused:
        buttons[label + ' ✗'].tap()
        assert panel.status.markup == "<span size='large'>%s</span>" % (ENDSTOP % label)
    tapped = set(COMMANDS) - refused
    for label in tapped:
        buttons[label].tap()
    sent = dict(panel.sent)
    assert set(sent) == {COMMANDS[label] for label in tapped}
    assert ('First pass: continue with 3 Current; after Current, come back here for the second '
            'pass') in sent['CHOPPER_TUNE MOTOR=AB SAVE=1']


def refuses_two_motors(source: str, cfg: str, command: str, monkeypatch) -> bool:
    """Whether the tool behind this macro, dry-run on a printer built from `cfg`, refuses a
    two-motor axis there."""
    front = klipper_front.Front(source, cfg)
    kl = front.connect()
    for module in (belts, current, demo, envelope, extruder, resonance_map, tune):
        for name, value in (('Klippy', lambda path: SimpleNamespace(connect=lambda: kl)),
                            ('find_socket', lambda explicit=None: '<front>')):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, value)
    macro, *params = command.split()
    parser = build_parser()
    args = parser.parse_args(_gcode_args([macro[len('CHOPPER_'):].lower().replace('_', '-')]
                                         + params + ['DRY_RUN=1'], boolean_flags(parser)))
    try:
        dispatch(args)
    except SystemExit as refusal:
        return 'not on two-motor axes yet (#129)' in str(refusal.code)
    return False


@pytest.mark.parametrize('source', SOURCES)
@pytest.mark.parametrize('cfg', [awd_cfg('voron-2209'), dual_y('voron-2209'),
                                 awd_cfg('voron-2209', twins=False)], ids=['awd', 'dual-y', 'one-motor'])
def test_a_button_is_marked_for_two_motors_exactly_where_its_tool_refuses_them(built_panel, source,
                                                                             cfg, monkeypatch):
    # the panel repeats the tools' rule, and a tool that learns a rail (#129) must lose its
    # mark; these printers home on switches, so only that rule marks a button
    require(source)
    commands = built_panel(awd_cfg('voron-2209', twins=False)).commands
    marked = {text[:-len(' ✗')] for text in buttons_of(built_panel(cfg)) if text.endswith(' ✗')}
    assert marked == {label for label, command in commands.items()
                      if label not in ('Restore', 'Stop')
                      and refuses_two_motors(source, cfg, command, monkeypatch)}
