"""A G28 override with a z_hop ([safe_z_home], RatOS, Beacon) lifts an UNHOMED Z on every
X/Y homing and leaves it unhomed. Once M18 had unhomed Z, a job re-homing X/Y again and
again climbed the gantry into the frame (stock Voron 2.4 / Trident: z_hop 10). The tools
keep Z homed, refuse to start on an unhomed Z, and never lift it blindly."""
import inspect
from types import SimpleNamespace

import pytest

import chopper_autotune.collect as collect_mod
from chopper_autotune import extruder
from chopper_autotune.collect import (PARK_INTERVAL_MOVES, ZNotHomed, homing_z_hop, make_parker,
                                      park, refuse_blind_z_hop, rehome_unless_hot, release_gantry,
                                      run_restore)
from chopper_autotune.klippy import KlippyError

Z_HOP = 10.0
SAFE_Z_HOME = {'safe_z_home': {'z_hop': Z_HOP}}


@pytest.fixture(autouse=True)
def fresh_feature_cache(monkeypatch):
    monkeypatch.setattr(collect_mod, '_CLEAR_HOMING', {})


class SafeZHomeKl:
    """Klipper's homing state as a z_hop G28 override and stepper_enable change it."""

    def __init__(self, homed='', z=100.0, override=SAFE_Z_HOME, klipper_path=None):
        self.homed = set(homed)
        self.z = z
        self.override = override
        self.klipper_path = klipper_path
        self.blind_lifts = 0
        self.fail_next_homing = False
        self.scripts = []

    def settings(self):
        return dict({'stepper_x': {}, 'stepper_y': {}, 'stepper_z': {}}, **self.override)

    def homed_axes(self):
        return ''.join(axis for axis in 'xyz' if axis in self.homed)

    def stepper_states(self):
        return {'stepper_x': True, 'stepper_y': True, 'stepper_z': True, 'extruder': True}

    def info(self):
        return {'klipper_path': self.klipper_path}

    def gcode(self, script):
        self.scripts.append(script)
        for line in script.split('\n'):
            if line.startswith('G28'):
                if 'z' not in self.homed:           # the override: always hop, stay unhomed
                    self.z += Z_HOP
                    self.blind_lifts += 1
                elif self.z < Z_HOP:
                    self.z = Z_HOP
                if self.fail_next_homing:           # Klipper: motor_off() on a failed G28
                    self.fail_next_homing = False
                    self.homed.clear()
                    raise KlippyError('No trigger on x after full movement')
                self.homed |= set('xyz' if line == 'G28' else line[4:].lower().replace(' ', ''))
            elif line == 'M18':
                self.homed.clear()
            elif line.startswith('SET_KINEMATIC_POSITION') and 'CLEAR_HOMED=XY' in line:
                self.homed -= set('xy')


def run_a_job(kl, moves=PARK_INTERVAL_MOVES * 2 + 1):
    refuse_blind_z_hop(kl, kl.settings())
    hw = SimpleNamespace(center=(150.0, 150.0), axis_span=300.0)
    try:
        park(kl, hw)
        before_move = make_parker(kl, hw)
        for _ in range(moves):
            before_move(1, 1.0)                     # periodic mid-run re-homes
    finally:
        run_restore(lambda: rehome_unless_hot(kl))


@pytest.mark.parametrize('override', [
    SAFE_Z_HOME,
    {'ratos_homing': {'z_hop': 15.0}},
    {'beacon': {'home_xy_position': [150, 150], 'home_z_hop': 5.0}},
])
def test_a_job_on_an_unhomed_z_is_refused_before_any_motion(override):
    # homing Z here would lower the nozzle onto whatever stands on the bed
    kl = SafeZHomeKl(homed='', override=override)
    with pytest.raises(ZNotHomed, match='clear the bed, run G28'):
        run_a_job(kl)
    assert kl.scripts == [] and kl.blind_lifts == 0


def test_the_hop_is_found_in_every_known_g28_override():
    assert homing_z_hop(SAFE_Z_HOME) == Z_HOP
    assert homing_z_hop({'ratos_homing': {'z_hop': 15}}) == 15
    assert homing_z_hop({'beacon': {'home_xy_position': [1, 2], 'home_z_hop': 5}}) == 5
    assert homing_z_hop({'beacon': {'home_z_hop': 5}}) == 0     # no G28 takeover without it
    assert homing_z_hop({}) == 0


def test_a_job_on_a_homed_z_never_lifts_blind():
    kl = SafeZHomeKl(homed='xyz', z=40.0)
    run_a_job(kl)
    assert kl.blind_lifts == 0 and kl.z == 40.0
    assert not any('M18' in script for script in kl.scripts)


def test_plan_steps_in_a_row_keep_z_where_it_was(tmp_path):
    # tune, current, tune again, belts between them: before, every step's M18 unhomed Z
    # and every later G28 X Y lifted it by z_hop
    (tmp_path / 'klippy' / 'extras').mkdir(parents=True)
    (tmp_path / 'klippy' / 'extras' / 'force_move.py').write_text("CLEAR_HOMED")
    kl = SafeZHomeKl(homed='xyz', z=40.0, klipper_path=str(tmp_path))
    for _ in range(3):
        run_a_job(kl)
        release_gantry(kl)                           # belts hands the gantry over
        kl.homed |= set('xy')                        # the next job homes X/Y first
    assert kl.blind_lifts == 0 and kl.z == 40.0


def test_a_failed_mid_run_homing_stops_instead_of_climbing():
    # Klipper answers a failed G28 with motor_off(): Z is unhomed from then on
    kl = SafeZHomeKl(homed='xyz', z=40.0)
    hw = SimpleNamespace(center=(150.0, 150.0), axis_span=300.0)
    park(kl, hw)
    before_move = make_parker(kl, hw)
    for index in range(PARK_INTERVAL_MOVES):
        before_move(1 if index % 2 else -1, 1.0)     # no drift: re-home on the count
    kl.fail_next_homing = True
    with pytest.raises(KlippyError):
        before_move(1, 1.0)                          # the periodic re-home fails
    with pytest.raises(ZNotHomed):
        try:
            before_move(1, 1.0)                      # a retry must not home again
        finally:
            run_restore(lambda: rehome_unless_hot(kl))
    assert kl.blind_lifts == 0
    # Klipper's motor_off counts the motors as off, the restore may have re-energized them:
    # each is enabled, then disabled, so Klipper really writes toff=0
    assert kl.scripts[-1].startswith('SET_STEPPER_ENABLE STEPPER="stepper_x" ENABLE=1\n'
                                     'SET_STEPPER_ENABLE STEPPER="stepper_x" ENABLE=0')
    assert 'STEPPER="extruder" ENABLE=1' not in kl.scripts[-1]   # no registers written there


def test_the_extruder_tools_never_switch_z_off():
    # M84 is M18: it unhomes Z for the next job; the extruder moves only its own motor
    source = inspect.getsource(extruder)
    assert "'M84'" not in source and "'M18'" not in source
    assert "SET_STEPPER_ENABLE STEPPER=extruder ENABLE=0" in source


@pytest.mark.parametrize('supported, tail', [(True, ' SET_HOMED=X'), (False, '')])
def test_the_referee_marks_only_its_own_axis_homed(tmp_path, supported, tail):
    from chopper_autotune.current import Referee
    (tmp_path / 'klippy' / 'extras').mkdir(parents=True)
    (tmp_path / 'klippy' / 'extras' / 'force_move.py').write_text(
        "CLEAR_HOMED" if supported else "homing_axes=(0, 1, 2)")
    kl = SafeZHomeKl(homed='xyz', z=40.0, klipper_path=str(tmp_path))
    kl.request = lambda method, params=None: {'stepper_x': 'TRIGGERED'}
    settings = {'stepper_x': {'endstop_pin': 'PA1', 'position_endstop': 0, 'position_max': 300}}
    Referee(kl, 'x', settings, 150.0).slipped()
    assert 'SET_KINEMATIC_POSITION X=28.000%s' % tail in kl.scripts


def test_the_refusal_keeps_its_instruction_on_the_display():
    # announce_failure shows '<command> FAILED: <message>' cut at 120 characters
    kl = SafeZHomeKl(homed='xy')
    with pytest.raises(ZNotHomed) as refused:
        refuse_blind_z_hop(kl, kl.settings())
    assert 'run G28, then retry' in ('find-speed FAILED: %s' % refused.value.code)[:120]


def test_the_xy_homing_is_cleared_only_with_every_axis_homed(tmp_path):
    # code older than the file on disk (updated, not restarted) marks ALL axes homed with
    # that command: harmless only when all three already are
    (tmp_path / 'klippy' / 'extras').mkdir(parents=True)
    (tmp_path / 'klippy' / 'extras' / 'force_move.py').write_text("CLEAR_HOMED")
    kl = SafeZHomeKl(homed='xy', klipper_path=str(tmp_path))
    release_gantry(kl)
    assert 'SET_KINEMATIC_POSITION' not in kl.scripts[-1]


def test_a_release_without_a_clearable_homing_forgets_it_all_and_keeps_z_holding():
    # no CLEAR_HOMED (or Z unhomed): M84 forgets the stale X/Y homing, Z goes back on
    kl = SafeZHomeKl(homed='xy', override={})
    release_gantry(kl)
    assert kl.scripts[-1].endswith('M84\nSET_STEPPER_ENABLE STEPPER="stepper_z" ENABLE=1')


def test_only_gantry_and_head_motors_are_switched_off():
    # a cutter or an MMU lane is none of the tool's business; names with spaces get quotes
    from chopper_autotune.collect import motors_off_but_z
    kl = SafeZHomeKl(homed='xyz')
    kl.stepper_states = lambda: {'manual_stepper cutter': True, 'extruder_stepper belted': True,
                                 'stepper_x': True, 'stepper_y': True, 'stepper_z': True,
                                 'extruder': True}
    script = motors_off_but_z(kl)
    assert 'STEPPER="extruder_stepper belted" ENABLE=0' in script
    assert 'manual_stepper' not in script and 'stepper_z' not in script


def test_corexz_is_refused_its_x_motors_carry_z():
    from chopper_autotune.collect import refuse_multi_motor
    with pytest.raises(SystemExit, match='corexz: the X motors move Z too'):
        refuse_multi_motor({'printer': {'kinematics': 'corexz'}, 'stepper_x': {}, 'stepper_y': {}})
