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


@pytest.mark.parametrize('module, function, argv', [
    ('chopper_autotune.tune', 'run_tune', ['tune', '--dry-run']),
    ('chopper_autotune.demo', 'run_demo', ['demo', '--dry-run']),
])
def test_pipeline_entry_points_refuse_awd_first(monkeypatch, module, function, argv):
    # demo: before the per-motor loop, where a refusal would read as 'motor A skipped'
    import importlib
    target = importlib.import_module(module)
    kl = RecordingKl(AWD)
    monkeypatch.setattr(target, 'find_socket', lambda *args: '<sock>')
    monkeypatch.setattr(target, 'Klippy', lambda path: kl)
    with pytest.raises(SystemExit, match='several motors drive one axis'):
        getattr(target, function)(build_parser().parse_args(argv))
    assert kl.scripts == []


def test_release_gantry_switches_off_the_twins_too():
    kl = RecordingKl(AWD)
    release_gantry(kl)
    assert kl.scripts == ['\n'.join('SET_STEPPER_ENABLE STEPPER=%s ENABLE=0' % name for name in
                                    ('stepper_x', 'stepper_x1', 'stepper_y', 'stepper_y1'))]
