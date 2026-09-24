"""Several motors on one axis (AWD, a two-motor gantry, #129): the tools that move or
tune one motor act on stepper_x/stepper_y only, so they refuse before any G-code."""
import pytest

from chopper_autotune.cli import build_parser
from chopper_autotune.collect import rail_twins, refuse_multi_motor, release_gantry

AWD = {'stepper_x': {}, 'stepper_x1': {}, 'stepper_y': {}, 'stepper_y1': {}, 'stepper_z': {}}


class RecordingKl:
    path = '<sock>'

    def __init__(self, settings):
        self._settings = settings
        self.scripts = []

    def settings(self):
        return self._settings

    def gcode(self, script):
        self.scripts.append(script)

    def object_list(self):
        return []

    def connect(self, sock=None):
        return self

    def close(self):
        pass


def test_rail_twins_follows_klippers_rule():
    # klippy/stepper.py LookupMultiRail: stepper_x1, stepper_x2, ... up to the first gap
    settings = {'stepper_x': {}, 'stepper_x1': {}, 'stepper_x2': {}, 'stepper_x4': {}}
    assert rail_twins(settings, 'x') == ['stepper_x1', 'stepper_x2']
    assert rail_twins(settings, 'y') == []


def test_two_wheel_drive_passes_and_awd_is_refused():
    refuse_multi_motor({'stepper_x': {}, 'stepper_y': {}, 'stepper_z': {}, 'stepper_z1': {}})
    with pytest.raises(SystemExit, match='stepper_x1, stepper_y1: several motors drive one axis'):
        refuse_multi_motor(AWD)


@pytest.mark.parametrize('module, function, argv', [
    ('chopper_autotune.collect', 'collect', ['collect', '--speed', '55', '--dry-run']),
    ('chopper_autotune.find_speed', 'scan', ['find-speed', '--dry-run']),
    ('chopper_autotune.resonance_map', 'resonance_map', ['map', '--dry-run']),
    ('chopper_autotune.current', 'current_tune', ['current', '--dry-run']),
])
def test_single_motor_tools_refuse_awd_before_any_gcode(module, function, argv):
    import importlib
    kl = RecordingKl(AWD)
    with pytest.raises(SystemExit, match='several motors drive one axis'):
        getattr(importlib.import_module(module), function)(kl, build_parser().parse_args(argv))
    assert kl.scripts == []


def _never(*args, **kwargs):
    pytest.fail('the entry point went on to a per-motor step')


@pytest.mark.parametrize('module, function, argv, next_steps', [
    ('chopper_autotune.tune', 'run_tune', ['tune', '--dry-run'], ('scan', 'collect')),
    ('chopper_autotune.demo', 'run_demo', ['demo', '--dry-run'], ('showcase_together', 'demo')),
    ('chopper_autotune.demo', 'run_demo', ['demo', '--report', '--dry-run'], ('showcase_together', 'demo')),
])
def test_pipeline_entry_points_refuse_awd_first(monkeypatch, module, function, argv, next_steps):
    # refused at the entry itself: demo's per-motor loop would read it as 'motor A skipped'
    import importlib
    target = importlib.import_module(module)
    kl = RecordingKl(AWD)
    monkeypatch.setattr(target, 'find_socket', lambda *args: '<sock>')
    monkeypatch.setattr(target, 'Klippy', lambda path: kl)
    for name in next_steps:
        monkeypatch.setattr(target, name, _never)
    with pytest.raises(SystemExit, match='several motors drive one axis'):
        getattr(target, function)(build_parser().parse_args(argv))
    assert kl.scripts == []


def test_only_the_driven_axes_count():
    # a dual-Y gantry can still tune X; tuning Y or both is refused
    dual_y = {'stepper_x': {}, 'stepper_y': {}, 'stepper_y1': {}}
    refuse_multi_motor(dual_y, 'x')
    for axes in ('y', 'xy'):
        with pytest.raises(SystemExit, match='stepper_y1'):
            refuse_multi_motor(dual_y, axes)


def test_envelope_note_names_the_twins_of_the_measured_motors():
    from chopper_autotune.envelope import awd_note
    assert 'stepper_x1' in awd_note(AWD, ['x']) and 'stepper_y1' not in awd_note(AWD, ['x'])
    assert awd_note({'stepper_x': {}, 'stepper_y': {}}, ['x', 'y']) == ''
    assert '"' not in awd_note(AWD, ['x', 'y'])              # travels inside RESPOND MSG="..."


def test_belt_jog_releases_the_gantry(monkeypatch):
    from types import SimpleNamespace

    from chopper_autotune.belts import identify_belt
    kl = RecordingKl(AWD)
    hw = SimpleNamespace(center=(60.0, 60.0), kinematics='corexy', axis_span=120.0)
    identify_belt(kl, hw, 'x', SimpleNamespace(update=lambda *args, **kwargs: None), cycles=1)
    assert kl.scripts[-1].startswith('M18\n')


def test_release_gantry_forgets_the_homing_and_keeps_z_holding():
    # hands move the head next: M18 is the one motor-off that clears the homing in every
    # Klipper version; Z (all of its motors) goes straight back on
    kl = RecordingKl(dict(AWD, stepper_z1={}))
    release_gantry(kl)
    assert kl.scripts == ['M18\nSET_STEPPER_ENABLE STEPPER=stepper_z ENABLE=1\n'
                          'SET_STEPPER_ENABLE STEPPER=stepper_z1 ENABLE=1']
