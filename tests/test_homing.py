"""[safe_z_home] lifts an UNHOMED Z by z_hop on every G28 X Y and leaves it unhomed: a job
that re-homes X/Y again and again climbed the gantry into the frame once M18 had unhomed Z
(stock Voron 2.4 / Trident: z_hop 10). The tools keep Z homed and home all once if needed."""
from types import SimpleNamespace

import chopper_autotune.collect as collect_mod
from chopper_autotune.collect import (PARK_INTERVAL_MOVES, ensure_z_homed, make_parker, park,
                                      release_gantry, rehome_unless_hot)

Z_HOP = 10.0


class SafeZHomeKl:
    """Klipper's homing state as [safe_z_home] (z_hop) and stepper_enable change it."""

    def __init__(self, homed='', z=100.0):
        self.homed = set(homed)
        self.z = z
        self.blind_lifts = 0
        self.scripts = []

    def settings(self):
        return {'stepper_x': {}, 'stepper_y': {}, 'stepper_z': {}, 'safe_z_home': {'z_hop': Z_HOP}}

    def homed_axes(self):
        return ''.join(axis for axis in 'xyz' if axis in self.homed)

    def gcode(self, script):
        self.scripts.append(script)
        for line in script.split('\n'):
            if line in ('G28', 'G28 X Y'):
                if 'z' not in self.homed:           # safe_z_home: always hop, stay unhomed
                    self.z += Z_HOP
                    self.blind_lifts += 1
                elif self.z < Z_HOP:
                    self.z = Z_HOP
                self.homed |= set('xyz' if line == 'G28' else 'xy')
            elif line == 'M18':
                self.homed.clear()
            elif line.startswith('SET_KINEMATIC_POSITION') and 'CLEAR_HOMED=XY' in line:
                self.homed -= set('xy')


def run_a_job(kl, moves=PARK_INTERVAL_MOVES * 2 + 1):
    ensure_z_homed(kl, kl.settings())
    hw = SimpleNamespace(center=(150.0, 150.0), axis_span=300.0)
    park(kl, hw)
    before_move = make_parker(kl, hw)
    for _ in range(moves):
        before_move(1, 1.0)                         # periodic mid-run re-homes
    rehome_unless_hot(kl)


def test_a_job_on_an_unhomed_z_homes_all_once_then_never_lifts_blind():
    kl = SafeZHomeKl(homed='', z=100.0)
    run_a_job(kl)
    assert kl.scripts[0] == 'G28\nM400'
    assert kl.blind_lifts == 1                       # the full G28's own hop, once
    assert 'z' in kl.homed


def test_a_job_on_a_homed_z_adds_no_homing_and_no_lift():
    kl = SafeZHomeKl(homed='xyz', z=40.0)
    run_a_job(kl)
    assert kl.blind_lifts == 0 and kl.z == 40.0
    assert not any(line == 'G28' for script in kl.scripts for line in script.split('\n'))


def test_plan_steps_in_a_row_keep_z_where_it_was(monkeypatch, tmp_path):
    # tune, current, tune again, belts between them: before, every step's M18 unhomed Z
    # and every later G28 X Y lifted it by z_hop
    (tmp_path / 'extras').mkdir()
    (tmp_path / 'extras' / 'force_move.py').write_text("CLEAR_HOMED")
    monkeypatch.setattr(collect_mod, 'KLIPPY_DIR', str(tmp_path))
    kl = SafeZHomeKl(homed='xyz', z=40.0)
    for _ in range(3):
        run_a_job(kl)
        release_gantry(kl)                           # belts hands the gantry over
    assert kl.blind_lifts == 0 and kl.z == 40.0
