import os
import re
import time
from datetime import datetime

import pytest

from chopper_autotune import tmc
from chopper_autotune.analyze import run_save, updated_config

CFG = """[include mainsail.cfg]

[tmc2209 stepper_x]
uart_pin: PC11
run_current: 1.8
# driver_TBL: 0
driver_TOFF: 3
driver_hend: 13

[tmc2209 stepper_y]
uart_pin: PC11
driver_TOFF: 5
"""

FIELDS = {'tbl': 0, 'toff': 8, 'hstrt': 7, 'hend': 5}


def test_updated_config_replaces_and_inserts():
    out = updated_config(CFG, 'tmc2209 stepper_x', FIELDS)
    x_section = out.split('[tmc2209 stepper_y]')[0]
    assert 'driver_TBL: 0\ndriver_TOFF: 8\ndriver_HSTRT: 7\ndriver_HEND: 5\n' in x_section
    assert '# driver_TBL: 0' in x_section          # commented history is kept
    assert x_section.count('driver_TOFF') == 1     # active line replaced, not duplicated
    assert 'driver_hend: 13' not in x_section      # replaced case-insensitively
    assert 'run_current: 1.8' in x_section
    # the other section is untouched
    assert 'driver_TOFF: 5' in out.split('[tmc2209 stepper_y]')[1]


def test_updated_config_errors():
    with pytest.raises(SystemExit, match='not found'):
        updated_config(CFG, 'tmc2209 stepper_z', FIELDS)
    doubled = CFG + '\n[tmc2209 stepper_x]\nuart_pin: PC10\n'
    with pytest.raises(SystemExit, match='2 times'):
        updated_config(doubled, 'tmc2209 stepper_x', FIELDS)


class FakeMoonraker:
    def __init__(self, files, printing=False, settings=None, commands=None):
        self.files = dict(files)
        self.printing = printing
        self.config_settings = settings or {}
        self.commands = commands
        self.uploads = []
        self.scripts = []

    def settings(self):
        return self.config_settings

    def accepted_commands(self):
        return self.commands

    def is_printing(self):
        return self.printing

    def list_config_files(self):
        return list(self.files)

    def download_config(self, name):
        return self.files[name]

    def upload_config(self, name, content):
        self.uploads.append(name)
        self.files[name] = content

    def gcode(self, script):
        self.scripts.append(script)

    def set_tmc_fields(self, stepper, fields):             # as Moonraker sends it
        self.gcode(tmc.set_fields_script(stepper, fields))


def test_run_save_backs_up_edits_and_restarts(capsys):
    mk = FakeMoonraker({'printer.cfg': CFG, 'mainsail.cfg': '[respond]\n'})
    run_save(mk, [({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))])

    assert mk.uploads == ['printer.chopper-backup.cfg', 'printer.cfg']
    assert mk.files['printer.chopper-backup.cfg'] == CFG
    assert 'driver_TOFF: 8' in mk.files['printer.cfg']
    assert mk.scripts == ['RESTART']
    assert 'Saved the new registers to printer.cfg' in capsys.readouterr().out


def test_run_save_refuses_when_printing():
    mk = FakeMoonraker({'printer.cfg': CFG}, printing=True)
    with pytest.raises(SystemExit, match='busy printing'):
        run_save(mk, [({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))])
    assert mk.uploads == []


def test_run_save_refuses_a_winner_klipper_would_not_load():
    # a TMC2660 winner from an older dataset with raw hstrt + hend 16..18: saving it
    # would keep Klipper from starting, so nothing is written
    mk = FakeMoonraker({'printer.cfg': CFG.replace('tmc2209', 'tmc2660')})
    with pytest.raises(SystemExit, match='Klipper refuses'):
        run_save(mk, [({'driver': '2660', 'stepper': 'stepper_x'}, tmc.Chopper(2, 4, 7, 9))])
    assert mk.uploads == [] and mk.scripts == []


def test_run_save_refuses_genuinely_ambiguous_sections():
    # both files are actually loaded (printer.cfg includes extra.cfg) and both carry
    # the section -> genuine ambiguity, refuse
    files = {'printer.cfg': '[include extra.cfg]\n' + CFG,
             'extra.cfg': '[tmc2209 stepper_x]\nrun_current: 1\n'}
    mk = FakeMoonraker(files)
    with pytest.raises(SystemExit, match='several files'):
        run_save(mk, [({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))])


def test_run_save_ignores_unincluded_dated_backups():
    # Mainsail/SAVE_CONFIG leftovers carry the same section but are not [include]d;
    # they must not block or receive the save
    files = {'printer.cfg': CFG,
             'mainsail.cfg': '[respond]\n',
             'printer-20250922_211125.cfg': CFG,
             'printer-20260307_201242.cfg': CFG}
    mk = FakeMoonraker(files)
    run_save(mk, [({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))])
    assert mk.uploads == ['printer.chopper-backup.cfg', 'printer.cfg']
    assert 'driver_TOFF: 8' in mk.files['printer.cfg']
    # the dated backups are untouched
    assert mk.files['printer-20250922_211125.cfg'] == CFG


def test_run_save_ignores_its_own_backups():
    mk = FakeMoonraker({'printer.cfg': CFG})
    item = [({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))]
    run_save(mk, item)
    mk.uploads.clear()
    # the backup now sits in the config root with the same section: must not confuse a re-save
    run_save(mk, item)
    assert mk.uploads == ['printer.chopper-backup.cfg', 'printer.cfg']


def test_run_save_uploads_all_backups_before_edits():
    files = {'printer.cfg': '[include extra.cfg]\n' + CFG,
             'extra.cfg': '[tmc2209 stepper_z]\nuart_pin: PC10\ndriver_TOFF: 4\n'}
    mk = FakeMoonraker(files)
    run_save(mk, [
        ({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5)),
        ({'driver': '2209', 'stepper': 'stepper_z'}, tmc.Chopper(1, 6, 5, 4)),
    ])
    assert mk.uploads[:2] == ['printer.chopper-backup.cfg', 'extra.chopper-backup.cfg']
    assert set(mk.uploads[2:]) == {'printer.cfg', 'extra.cfg'}
    assert mk.scripts == ['RESTART']
    assert 'driver_TOFF: 6' in mk.files['extra.cfg']


CONFIG_WITH_TUNING = """[tmc2209 stepper_x]
uart_pin: PA1
run_current: 1.0
driver_TBL: 0
driver_TOFF: 2
driver_HSTRT: 7
driver_HEND: 11

[tmc2209 stepper_y]
uart_pin: PA2
run_current: 1.0

[tmc2209 extruder]
uart_pin: PA3
run_current: 0.65
driver_TBL: 3
driver_TOFF: 7
driver_HSTRT: 6
driver_HEND: 0
"""


def test_tuned_tmc_sections_finds_only_tuned_motors():
    from chopper_autotune.analyze import tuned_tmc_sections
    sections = tuned_tmc_sections({'printer.cfg': CONFIG_WITH_TUNING})
    # stepper_y carries no driver_* lines: already stock, must not be rewritten
    assert sections == ['tmc2209 stepper_x', 'tmc2209 extruder']


class FakeMk:
    def __init__(self, files):
        self.files = dict(files)
        self.gcodes = []
        self.printing = False

    def is_printing(self):
        return self.printing

    def list_config_files(self):
        return list(self.files)

    def download_config(self, name):
        return self.files[name]

    def upload_config(self, name, content):
        self.files[name] = content

    def gcode(self, script):
        self.gcodes.append(script)


def test_restore_defaults_rewrites_tuned_sections_only(monkeypatch):
    from types import SimpleNamespace

    import chopper_autotune.analyze as analyze
    mk = FakeMk({'printer.cfg': CONFIG_WITH_TUNING})
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    analyze.run_restore_config(SimpleNamespace(defaults=True, backup=False, url=''))
    text = mk.files['printer.cfg']
    x = text[text.index('[tmc2209 stepper_x]'):text.index('[tmc2209 stepper_y]')]
    assert 'driver_TBL: 2' in x and 'driver_HEND: 0' in x    # stock registers written
    assert 'run_current: 1.0' in x                           # the current is untouched
    assert mk.files['printer.chopper-backup.cfg']            # snapshot taken first
    assert mk.gcodes == ['RESTART']


def test_restore_backup_puts_snapshots_back(monkeypatch):
    from types import SimpleNamespace

    import chopper_autotune.analyze as analyze
    mk = FakeMk({'printer.cfg': 'edited', 'printer.chopper-backup.cfg': 'pristine'})
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    analyze.run_restore_config(SimpleNamespace(defaults=False, backup=True, url=''))
    assert mk.files['printer.cfg'] == 'pristine'
    assert mk.gcodes == ['RESTART']


def test_restore_needs_exactly_one_mode(monkeypatch):
    from types import SimpleNamespace

    import pytest

    import chopper_autotune.analyze as analyze
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: FakeMk({}))
    with pytest.raises(SystemExit, match='pick one'):
        analyze.run_restore_config(SimpleNamespace(defaults=False, backup=False, url=''))


def test_restore_resets_the_plan_marks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import chopper_autotune.analyze as analyze
    monkeypatch.setattr(analyze, 'RESULTS_HOME', tmp_path)
    for name in ('belts.json', 'current.json', 'envelope.json', 'map.json',
                 'state.json', 'extruder.json'):
        (tmp_path / name).write_text('{}')
    mk = FakeMk({'printer.cfg': CONFIG_WITH_TUNING})
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    analyze.run_restore_config(SimpleNamespace(defaults=True, backup=False, url=''))
    # thresholds/ceilings/map were measured against the rolled-away registers
    for name in ('belts.json', 'current.json', 'envelope.json', 'map.json', 'state.json'):
        assert not (tmp_path / name).exists(), name
    # the runs stay; a save skips them by the time of the rollback
    assert (tmp_path / 'extruder.json').exists()


CONFIG_2240_TUNED = """[tmc2240 stepper_x]
cs_pin: PA1
run_current: 1.0
driver_TBL: 1
driver_TOFF: 8
driver_HSTRT: 4
driver_HEND: 8
driver_TPFD: 14

[tmc2240 stepper_y]
cs_pin: PA2
run_current: 1.0
driver_TPFD: 7
"""


def test_restore_defaults_writes_the_drivers_own_stock_registers(monkeypatch):
    from types import SimpleNamespace

    import chopper_autotune.analyze as analyze
    mk = FakeMk({'printer.cfg': CONFIG_2240_TUNED})
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    analyze.run_restore_config(SimpleNamespace(defaults=True, backup=False, url=''))
    text = mk.files['printer.cfg']
    x = text[text.index('[tmc2240 stepper_x]'):text.index('[tmc2240 stepper_y]')]
    # the 2240 stock is hend 2 / tpfd 4, not the 2209's hend 0 — and the tuned TPFD
    # line must not survive a "restore" that claims stock registers
    assert 'driver_HEND: 2' in x and 'driver_TPFD: 4' in x
    assert 'driver_TPFD: 14' not in x and 'driver_HEND: 0' not in x


def test_tuned_tmc_sections_sees_a_tpfd_only_tuning():
    from chopper_autotune.analyze import tuned_tmc_sections
    assert tuned_tmc_sections({'printer.cfg': CONFIG_2240_TUNED}) \
        == ['tmc2240 stepper_x', 'tmc2240 stepper_y']


def test_rank_never_recommends_a_combo_klipper_would_not_load():
    from chopper_autotune.analyze import rank
    rows = [{'chopper': tmc.Chopper(2, 4, 7, 9), 'magnitude': 10.0, 'n': 2},    # raw 16
            {'chopper': tmc.Chopper(2, 4, 7, 8), 'magnitude': 50.0, 'n': 2}]
    assert [r['chopper'] for r in rank(rows, tmc.DRIVERS['2660'], tmc.Hearing())] == [tmc.Chopper(2, 4, 7, 8)]
    assert len(rank(rows, tmc.DRIVERS['2209'], tmc.Hearing())) == 2


RAIL_CFG = CFG + """
[tmc2209 stepper_x1]
uart_pin: PC12
driver_TOFF: 4
"""
RAIL = {'driver': '2209', 'stepper': 'stepper_x', 'motion': 'rail', 'steppers': ['stepper_x', 'stepper_x1']}
RAIL_SETTINGS = {'stepper_x': {}, 'stepper_x1': {}, 'tmc2209 stepper_x': {},
                 'tmc2209 stepper_x1': {}}


def test_run_save_writes_a_rails_winner_into_each_of_its_sections_in_one_restart():
    # AWD: stepper_x1 drives the same belt (#129), the rail run measured both drivers
    mk = FakeMoonraker({'printer.cfg': RAIL_CFG}, settings=RAIL_SETTINGS)
    run_save(mk, [(RAIL, tmc.Chopper(0, 8, 7, 5))])
    assert mk.uploads == ['printer.chopper-backup.cfg', 'printer.cfg']
    assert mk.scripts == ['RESTART']
    text = mk.files['printer.cfg']
    for section in ('[tmc2209 stepper_x]\n', '[tmc2209 stepper_x1]\n'):
        assert section + 'driver_TBL: 0\ndriver_TOFF: 8\ndriver_HSTRT: 7\ndriver_HEND: 5\n' in text
    assert text.count('driver_TOFF: 8') == 2 and 'driver_TOFF: 4' not in text
    assert 'driver_TOFF: 5' in text.split('[tmc2209 stepper_y]')[1]     # motor B untouched


@pytest.mark.parametrize('settings, manifest, refusal', [
    # a twin with no section of the model the rail measured: no save of half the rail
    ({'stepper_x': {}, 'stepper_x1': {}, 'tmc2209 stepper_x': {}}, RAIL,
     'tune motor A again: its dataset measured TMC2209 drivers, the config gives stepper_x1 no '
     'TMC section now'),
    # the drivers swapped for another model since the run
    (RAIL_SETTINGS, dict(RAIL, driver='5160'),
     'its dataset measured TMC5160 drivers, the config gives stepper_x TMC2209, stepper_x1 '
     'TMC2209'),
    # a dataset of stepper_x alone would leave the twin on its old registers
    ({'stepper_x': {}, 'stepper_x1': {}}, {'driver': '2209', 'stepper': 'stepper_x'},
     'tune motor A again: its dataset measured stepper_x alone, the config drives the axis with '
     'stepper_x, stepper_x1 now'),
    # the rail changed since the run
    ({'stepper_x': {}}, RAIL,
     'tune motor A again: its dataset measured stepper_x, stepper_x1, the config drives the axis '
     'with stepper_x alone now'),
    ({'stepper_x': {}, 'stepper_x1': {}, 'stepper_x2': {}}, RAIL, 'tune motor A again'),
])
def test_run_save_refuses_a_dataset_of_another_rail_than_the_configs(settings, manifest, refusal):
    mk = FakeMoonraker({'printer.cfg': RAIL_CFG}, settings=settings)
    with pytest.raises(SystemExit, match=re.escape(refusal)):
        run_save(mk, [(manifest, tmc.Chopper(0, 8, 7, 5))])
    assert mk.uploads == [] and mk.scripts == []


def test_apply_sets_every_driver_of_a_rail_and_refuses_one_of_them(capsys):
    # the rail's motors go on first, then every driver gets the set in one script, as a run
    # writes it; a twin without an enable pin of its own gets the warning, its main motor
    # does not
    from chopper_autotune.analyze import run_apply
    mk = FakeMoonraker({}, settings=dict(RAIL_SETTINGS, stepper_x={'enable_pin': '!PA1'}))
    combo = tmc.Chopper(0, 8, 7, 5)
    with pytest.raises(SystemExit, match='tune motor A again: its dataset measured stepper_x alone'):
        run_apply(mk, '2209', ['stepper_x'], combo)
    assert mk.scripts == []
    run_apply(mk, '2209', ['stepper_x', 'stepper_x1'], combo)
    assert mk.scripts[:2] == ['SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=1\n'
                              'SET_STEPPER_ENABLE STEPPER=stepper_x1 ENABLE=1',
                              tmc.set_fields_script('stepper_x', combo.fields()) + '\n'
                              + tmc.set_fields_script('stepper_x1', combo.fields())]
    assert [script for script in mk.scripts if script.startswith('M118')] \
        == ['M118 WARNING: stepper_x1 has no enable pin of its own: at the next motors off and on '
            'Klipper puts toff back to the config value and keeps the other applied registers; '
            'SAVE=1 keeps the whole set']
    assert 'stepper_x has no enable pin' not in capsys.readouterr().out


@pytest.mark.parametrize('enable_pins, shared', [
    ({'stepper_x': '!PA1', 'stepper_y': '!PA2'}, False),
    ({'stepper_x': '!PA1', 'stepper_y': 'PA1'}, True),          # one EN line for both
    ({}, True),                                                 # no enable pin at all
])
def test_apply_enables_the_motor_before_its_registers(enable_pins, shared, capsys):
    # toff written to a motor Klipper counts as off energizes it, and Klipper's own enable
    # would put its config toff back; a shared enable does that at every motors off and on
    from chopper_autotune.analyze import run_apply
    settings = {name: {'enable_pin': pin} for name, pin in enable_pins.items()}
    mk = FakeMoonraker({}, settings=dict({'stepper_x': {}}, **settings))
    run_apply(mk, '2209', ['stepper_x'], tmc.Chopper(2, 3, 5, 0))
    assert mk.scripts[0] == 'SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=1'
    assert mk.scripts[1].startswith('SET_TMC_FIELD')
    assert ('no enable pin of its own' in capsys.readouterr().out) is shared
    # APPLY runs detached: the warning reaches the console too, not just the log
    assert (mk.scripts[-1].startswith('M118 WARNING: stepper_x has no enable pin')) is shared


def test_apply_sends_its_warning_only_where_klipper_takes_m118(capsys):
    # without [respond] the M118 drew an 'Unknown command' line and a KlipperScreen error
    from chopper_autotune.analyze import run_apply
    mk = FakeMoonraker({}, settings={'stepper_x': {}}, commands={'M117', 'SET_TMC_FIELD'})
    run_apply(mk, '2209', ['stepper_x'], tmc.Chopper(2, 3, 5, 0))
    assert 'no enable pin of its own' in capsys.readouterr().out
    assert not any(script.startswith('M118') for script in mk.scripts)


def test_moonraker_reads_the_commands_klipper_takes(monkeypatch):
    from chopper_autotune.moonraker import Moonraker, MoonrakerError
    mk = Moonraker('http://x')
    monkeypatch.setattr(mk, '_request', lambda method, path, params=None: {
        'status': {'gcode': {'commands': {'M117': {}, 'RESPOND': {}}}}})
    assert mk.accepted_commands() == {'M117', 'RESPOND'}

    def down(method, path, params=None):
        raise MoonrakerError('cannot reach Moonraker')
    monkeypatch.setattr(mk, '_request', down)
    assert mk.accepted_commands() is None


def test_apply_refuses_while_printing():
    from chopper_autotune.analyze import run_apply
    mk = FakeMoonraker({}, printing=True, settings={'stepper_x': {}})
    with pytest.raises(SystemExit, match='busy printing'):
        run_apply(mk, '2209', ['stepper_x'], tmc.Chopper(2, 3, 5, 0))
    assert mk.scripts == []


def test_restore_defaults_resets_both_drivers_of_a_pair():
    # an AWD pair tuned by hand in both sections: a stock reset must reach the twin too,
    # or the two motors of one belt end up on different choppers
    from chopper_autotune.analyze import tuned_tmc_sections
    text = ('[tmc5160 stepper_x]\ndriver_TBL: 1\n\n[tmc5160 stepper_x1]\ndriver_TBL: 1\n\n'
            '[tmc5160 stepper_y]\nrun_current: 1.0\n')
    assert tuned_tmc_sections({'printer.cfg': text}) == ['tmc5160 stepper_x', 'tmc5160 stepper_x1']


AUTOTUNE = {'autotune_tmc stepper_x': {'motor': 'ldo-42sth48-2004mah', 'tuning_goal': 'performance'}}


def test_run_save_refuses_a_motor_klipper_tmc_autotune_manages():
    # autotune writes its own tbl/toff/hstrt/hend at every Klipper start: saved values
    # would never reach the driver, and the restart would be for nothing
    mk = FakeMoonraker({'printer.cfg': CFG}, settings=AUTOTUNE)
    with pytest.raises(SystemExit, match=r"autotune resets its chopper at start"):
        run_save(mk, [({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))])
    assert mk.uploads == [] and mk.scripts == []


@pytest.mark.parametrize('command, driver, stepper', [('tune', '2240', 'stepper_x'),
                                                       ('extruder', '5160', 'extruder'),
                                                       ('save', '2209', 'stepper_y')])
def test_the_autotune_refusal_points_the_display_at_the_log(command, driver, stepper):
    # announce_failure puts failure_display on the display: a bare "remove it" there would
    # drop the StallGuard threshold sensorless homing stops on
    from chopper_autotune.collect import autotune_refusal, failure_display
    shown = failure_display('%s FAILED: %s' % (command, autotune_refusal(driver, stepper)))
    assert 'the log says what to do' in shown and 'remove' not in shown


# the options each [tmc...] section reads, as Klipper records them in the settings
MASTER_2240 = {'driver_sgt': 0, 'driver_sg4_thrs': 0, 'driver_slope_control': 0}


@pytest.mark.parametrize('driver, section, reads, lines', [
    ('2209', {'sg4_thrs': 80}, {'driver_sgthrs': 0}, ['driver_SGTHRS: 80']),
    # SG4_THRS 0 too: it replaces an old line, and Klipper homes on SG4 when it is not 0
    ('2240', {'sgt': 2, 'sg4_thrs': 0}, MASTER_2240,
     ['driver_SGT: 2', 'driver_SG4_THRS: 0', 'driver_SLOPE_CONTROL: 3']),
    ('2240', {'sgt': 1, 'sg4_thrs': 60}, MASTER_2240,
     ['driver_SGT: 1', 'driver_SG4_THRS: 60', 'driver_SLOPE_CONTROL: 3']),
    # Klipper v0.13.0 has no sg4_thrs option: a line naming it stops Klipper at start
    ('2240', {'sgt': 1, 'sg4_thrs': 60}, {'driver_sgt': 0, 'driver_slope_control': 0},
     ['driver_SGT: 1', 'driver_SLOPE_CONTROL: 3']),
    ('5160', {'sgt': -4}, {'driver_sgt': 0}, ['driver_SGT: -4']),
    ('2208', {}, {}, []),
])
def test_the_advice_carries_over_what_autotune_sets(driver, section, reads, lines):
    # Klipper records autotune's defaults in the settings too; its own README moves the
    # StallGuard thresholds from [tmc...] into [autotune_tmc], so they must come back
    from chopper_autotune.collect import autotune_advice, autotune_carry_over
    settings = {'autotune_tmc stepper_x': section, 'tmc%s stepper_x' % driver: reads}
    assert autotune_carry_over(settings, driver, 'stepper_x') == lines
    advice = autotune_advice(settings, driver, 'stepper_x')
    assert all(line in advice for line in lines)
    # a TMC2208 has no CoolStep: its result stays good once autotune is off
    assert ('tune it again with SAVE=1, or save a result already measured' if driver == '2208'
            else 'tune again') in advice


def test_a_winner_measured_under_autotune_is_not_saved_once_it_is_gone():
    # its CoolStep lowers the current under load: the optimum was found at another current
    mk = FakeMoonraker({'printer.cfg': CFG}, settings={})
    with pytest.raises(SystemExit, match='measured under autotune; the log says what to do'):
        run_save(mk, [({'driver': '2209', 'stepper': 'stepper_x', 'autotune': 'performance'},
                       tmc.Chopper(0, 8, 7, 5))])
    assert mk.uploads == [] and mk.scripts == []


CFG_ABE = """[tmc2209 stepper_x]
uart_pin: PA1
driver_TOFF: 3

[tmc2209 stepper_y]
uart_pin: PA2
driver_TOFF: 3

[tmc2209 extruder]
uart_pin: PA3
driver_TOFF: 3
"""

A, B, E = tmc.Chopper(0, 8, 7, 5), tmc.Chopper(1, 6, 4, 2), tmc.Chopper(3, 7, 6, 0)


@pytest.fixture
def runs(tmp_path, monkeypatch):
    """runs(name, axis, winner, top=winner, age=0, kept=False, **manifest): a tuning run of
    motor A (axis x) or B (y) that CHOPPER_SAVE finds, written `age` seconds ago. `top`
    is its rank 1; without a winner the run recorded none, without either it measured
    nothing."""
    from chopper_autotune import analyze
    from chopper_autotune.dataset import Dataset
    base = tmp_path / 'datasets'
    listing = analyze.dataset_dirs
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: listing((base,)))

    def run(name, axis, winner=None, top=None, age=0.0, kept=False, **manifest):
        ds = Dataset.create(base / name, dict({'axis': axis, 'search': 'descent', 'driver': '2209',
                                               'stepper': 'stepper_' + axis, 'trim': 0.1}, **manifest))
        if winner:
            ds.update_manifest(winner=winner.fields(), kept=kept)
        if top or winner:
            for combo, magnitude in ((top or winner, 900.0), (tmc.Chopper(2, 3, 5, 0), 1000.0)):
                ds.append({'id': combo.label(), 'kind': 'move', 'status': 'ok', **combo.fields(),
                           'score': {'median_magnitude': magnitude}})
        when = time.time() - age
        for path in (ds.manifest_path, ds.records_path):
            if path.exists():
                os.utime(path, (when, when))
        return ds.root
    return run


def extruder_run(winner, age=0.0, driver='2209', autotune=None, hearing=tmc.Hearing()):
    """The winner CHOPPER_EXTRUDER stored, `age` seconds ago."""
    from chopper_autotune import extruder
    extruder.save_winner_state(driver, winner, autotune, hearing)
    when = time.time() - age
    os.utime(extruder.STATE, (when, when))


@pytest.fixture
def save(monkeypatch):
    """CHOPPER_SAVE with the macro's parameters against `mk` (default: printer.cfg = CFG_ABE)."""
    from chopper_autotune import analyze
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser

    def run(*params, mk=None):
        mk = mk or FakeMoonraker({'printer.cfg': CFG_ABE})
        monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
        parser = build_parser()
        analyze.run_save_latest(parser.parse_args(_gcode_args(['save', *params],
                                                              boolean_flags(parser))))
        return mk
    return run


def console(mk) -> 'list[str]':
    """What reached the Klipper console, line by line."""
    return [re.fullmatch(r'RESPOND PREFIX="Chopper:" MSG="(.*)"', line).group(1)
            for script in mk.scripts for line in script.split('\n') if line.startswith('RESPOND')]


def changed(mk) -> 'set[str]':
    """The sections of CFG_ABE whose registers a save rewrote."""
    text = mk.files['printer.cfg']
    return {name for name in ('stepper_x', 'stepper_y', 'extruder')
            if re.search(r'driver_TOFF: (\d+)', text.split('[tmc2209 %s]' % name)[1]).group(1) != '3'}


@pytest.mark.parametrize('motor, saved', [
    ('A', {'stepper_x'}), ('B', {'stepper_y'}), ('E', {'extruder'}), ('ae', {'stepper_x', 'extruder'}),
    (None, {'stepper_x', 'stepper_y', 'extruder'})])
def test_save_writes_only_the_motors_it_is_given(runs, save, motor, saved):
    # a tester who re-tuned motor A would otherwise write motor B's older, noisy result too
    runs('01_y', 'y', B, age=60)
    runs('02_x', 'x', A)
    extruder_run(E)
    mk = save(*(['MOTOR=' + motor] if motor else []))
    assert changed(mk) == saved
    assert mk.scripts[-1] == 'RESTART'


def test_the_console_names_the_registers_and_the_run_of_each_motor_before_the_restart(runs, save):
    # the save runs detached: its log is not seen, and the restart ends the session
    from chopper_autotune import extruder
    runs('01_y', 'y', B, age=60)
    runs('02_x', 'x', A)
    extruder_run(E, age=30)
    mk = save()
    when = datetime.fromtimestamp(os.path.getmtime(extruder.STATE)).strftime('%Y-%m-%d %H:%M')
    assert console(mk) == ['motor A: saving %s from run 02_x' % A.label(),
                           'motor B: saving %s from run 01_y' % B.label(),
                           'motor E: saving %s from the CHOPPER_EXTRUDER run of %s' % (E.label(), when)]
    assert mk.scripts[-1] == 'RESTART' and 'RESPOND' in mk.scripts[0]


@pytest.mark.parametrize('sync, commands', [(True, None), (False, {'M117', 'SET_TMC_FIELD'})])
def test_the_lines_reach_the_console_only_where_they_can(runs, save, monkeypatch, capsys, sync, commands):
    # a foreground run's macro holds Klipper's G-code queue, and its output reaches the
    # console anyway; without [respond] a RESPOND is an 'Unknown command' line
    runs('01_x', 'x', A)
    if sync:
        monkeypatch.setenv('CHOPPER_SYNC', '1')
    mk = save('MOTOR=A', 'DRY_RUN=1', mk=FakeMoonraker({'printer.cfg': CFG_ABE}, commands=commands))
    assert mk.scripts == []
    assert 'motor A: would save %s from run 01_x' % A.label() in capsys.readouterr().out


def test_a_dry_run_shows_the_same_lines_and_writes_nothing(runs, save):
    runs('01_y', 'y', B, age=60)
    runs('02_x', 'x', A)
    extruder_run(E, age=30)
    saved = save()
    dry = save('DRY_RUN=1')
    assert console(dry) == [line.replace(': saving ', ': would save ') for line in console(saved)] \
        + ['DRY_RUN=1: nothing written, Klipper keeps running']
    assert dry.uploads == [] and 'RESTART' not in dry.scripts


def test_a_motor_whose_run_kept_its_registers_is_not_rewritten(runs, save):
    # nothing to write for it: no backup, no restart
    own = tmc.Chopper(0, 2, 2, 12)
    runs('01_x', 'x', own, kept=True)
    runs('02_y', 'y', B, age=60)
    holding = {'tmc2209 stepper_x': {'driver_' + field: value for field, value in own.fields().items()}}
    mk = save(mk=FakeMoonraker({'printer.cfg': CFG_ABE}, settings=holding))
    assert changed(mk) == {'stepper_y'}
    assert console(mk)[0] == ('motor A: your registers %s stay. In run 01_x, neither the winner '
                              'nor Klipper defaults was quieter than them in all 4 rounds'
                              % own.label())
    mk = save('MOTOR=A', mk=FakeMoonraker({'printer.cfg': CFG_ABE}, settings=holding))
    assert mk.uploads == [] and 'RESTART' not in mk.scripts


def test_registers_a_run_kept_are_saved_once_the_config_holds_others(runs, save):
    # another save or a hand edit since: the motor's last result is not in the config
    own = tmc.Chopper(0, 2, 2, 12)
    runs('01_x', 'x', own, kept=True)
    mk = save('MOTOR=A', mk=FakeMoonraker({'printer.cfg': CFG_ABE},
                                          settings={'tmc2209 stepper_x': {'driver_toff': 3}}))
    assert 'driver_TOFF: 2' in mk.files['printer.cfg'].split('[tmc2209 stepper_y]')[0]
    assert console(mk) == ['motor A: saving %s from run 01_x' % own.label()]


@pytest.mark.parametrize('manifest', [{}, {'search': 'grid', 'validate': 0}])
def test_a_run_without_a_winner_is_not_saved_and_the_line_says_how_to_save_it(
        runs, save, monkeypatch, manifest):
    # stopped before its finalists were re-measured, or a grid with VALIDATE=0: its rank 1
    # was measured once; an older run of the motor is not what the person meant either
    from chopper_autotune import analyze, cli
    runs('01_x', 'x', B, age=60)
    root = runs('02_x', 'x', top=A, **manifest)
    mk = save('MOTOR=A')
    assert mk.uploads == [] and 'RESTART' not in mk.scripts
    command = 'CHOPPER_ANALYZE DATASET=%s SAVE=1' % root
    assert console(mk)[0].startswith('motor A: not saving run 02_x: it was stopped before the end, '
                                     'or it was a full sweep')
    assert console(mk)[0].endswith('anyway: ' + command)
    # the command in the line saves the run's rank 1 once the person asks for it
    analyzed = FakeMoonraker({'printer.cfg': CFG_ABE})
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: analyzed)
    assert cli.main(['analyze'] + command.split()[1:] + ['NO_HTML=1']) == 0
    assert 'driver_TOFF: %d' % A.toff in analyzed.files['printer.cfg'].split('[tmc2209 stepper_y]')[0]


def test_a_run_stopped_before_its_first_measurement_leaves_the_older_one_in_force(runs, save):
    # as collect writes it: the noise floor with the motors off, then a move that failed
    from chopper_autotune.dataset import Dataset
    runs('01_x', 'x', A, age=60)
    ds = Dataset(runs('02_x', 'x'))
    ds.append({'id': 'baseline', 'kind': 'baseline', 'status': 'ok', 'score': {'median_magnitude': 40.0}})
    ds.append({'id': B.label(), 'kind': 'move', 'status': 'failed', **B.fields(), 'error': 'timeout'})
    assert changed(save('MOTOR=A')) == {'stepper_x'}


def test_only_a_tuning_run_is_the_run_of_a_motor(runs, save):
    # a speed scan and a demo measure the motor too, but they choose no registers
    from chopper_autotune.dataset import Dataset
    base = runs('01_x', 'x', A, age=60).parent
    for name, mode in (('02_speed_x', 'find-speed'), ('03_demo_x', 'demo')):
        ds = Dataset.create(base / name, {'mode': mode, 'axis': 'x', 'stepper': 'stepper_x',
                                          'driver': '2209'})
        ds.append({'id': B.label(), 'kind': 'move', 'status': 'ok', **B.fields(),
                   'score': {'median_magnitude': 500.0}})
    mk = save('MOTOR=A')
    assert console(mk) == ['motor A: saving %s from run 01_x' % A.label()]


@pytest.mark.parametrize('backup', [False, True])
def test_runs_from_before_the_last_restore_are_not_saved(runs, save, monkeypatch, backup):
    # the next save must not write the rolled-back registers again
    from types import SimpleNamespace

    from chopper_autotune import analyze
    old = runs('01_x', 'x', A, age=60)
    extruder_run(E, age=60)
    files = {'printer.cfg': CFG_ABE, 'printer.chopper-backup.cfg': CFG_ABE}
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: FakeMoonraker(files))
    analyze.run_restore_config(SimpleNamespace(defaults=not backup, backup=backup, url=''))
    mk = save()
    assert console(mk) == ['motor A: not saving run 01_x: it is older than the last CHOPPER_RESTORE. '
                           'Saving it would undo that rollback. To save it anyway: '
                           'CHOPPER_ANALYZE DATASET=%s SAVE=1' % old,
                           'motor B: no tuning run to save',
                           'motor E: not saving the last CHOPPER_EXTRUDER run: it is older than the '
                           'last CHOPPER_RESTORE. Saving it would undo that rollback',
                           'Nothing to save, Klipper keeps running']
    assert mk.uploads == [] and 'RESTART' not in mk.scripts
    runs('02_x', 'x', B, age=-60)
    extruder_run(E, age=-60)
    assert changed(save()) == {'stepper_x', 'extruder'}


@pytest.mark.parametrize('files, printing, failure', [
    ({'printer.cfg': CFG_ABE}, True, 'busy printing'),
    ({'printer.cfg': CFG_ABE.replace('tmc2209 stepper_x', 'tmc2240 stepper_x')}, False,
     r'section \[tmc2209 stepper_x\] not found')])
def test_the_console_hears_of_a_save_only_once_it_can_write(runs, save, files, printing, failure):
    # 'saving' and then the failure would announce a write that never happens
    runs('01_x', 'x', A)
    mk = FakeMoonraker(files, printing=printing)
    with pytest.raises(SystemExit, match=failure):
        save('MOTOR=A', mk=mk)
    assert console(mk) == [] and mk.uploads == []


def test_a_refusal_reaches_the_console_once(runs, save):
    # as the failure itself: M118 from a detached run, stderr from a dry run
    runs('01_x', 'x', A)
    mk = FakeMoonraker({'printer.cfg': CFG_ABE}, settings=AUTOTUNE)
    with pytest.raises(SystemExit, match=r'^motor A: not saving \[tmc2209 stepper_x\]'):
        save('MOTOR=A', mk=mk)
    assert console(mk) == []


def test_a_test_reads_no_datasets_but_its_own(save, tmp_path, monkeypatch):
    # a developer's ./datasets (git ignores it) or the printer's own runs turned save tests red
    from chopper_autotune.dataset import Dataset
    checkout = tmp_path / 'checkout'
    ds = Dataset.create(checkout / 'datasets' / '01_x', {'axis': 'x', 'search': 'descent',
                                                         'driver': '2209', 'stepper': 'stepper_x',
                                                         'winner': A.fields()})
    ds.append({'id': A.label(), 'kind': 'move', 'status': 'ok', **A.fields(),
               'score': {'median_magnitude': 900.0}})
    monkeypatch.chdir(checkout)
    assert console(save('MOTOR=A')) == ['motor A: no tuning run to save',
                                        'Nothing to save, Klipper keeps running']


def test_with_nothing_to_save_klipper_keeps_running(save):
    mk = save()
    assert console(mk) == ['motor A: no tuning run to save', 'motor B: no tuning run to save',
                           'motor E: no CHOPPER_EXTRUDER run to save',
                           'Nothing to save, Klipper keeps running']
    assert mk.uploads == [] and 'RESTART' not in mk.scripts


def test_save_last_saves_what_save_motor_e_saves(runs, save, monkeypatch):
    from chopper_autotune import analyze, extruder
    from chopper_autotune.cli import build_parser
    runs('01_x', 'x', A)
    extruder_run(E)
    saved = save('MOTOR=E')
    mk = FakeMoonraker({'printer.cfg': CFG_ABE})
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    assert extruder.extruder_tune(None, build_parser().parse_args(['extruder', '--save-last'])) == 0
    assert console(mk) == console(saved) and mk.files == saved.files
    assert changed(mk) == {'extruder'}


def test_save_skips_a_motor_with_a_twin_and_saves_the_rest(runs, save):
    # AWD on X only: motor A would reach one driver of its pair; B and the extruder still save
    runs('01_x', 'x', A)
    runs('02_y', 'y', B)
    extruder_run(E)
    mk = save(mk=FakeMoonraker({'printer.cfg': CFG_ABE}, settings={'stepper_x': {}, 'stepper_x1': {}}))
    assert changed(mk) == {'stepper_y', 'extruder'}
    assert console(mk)[0].startswith('motor A: tune motor A again: its dataset measured stepper_x '
                                     'alone, the config drives the axis with stepper_x, stepper_x1 now')


def test_save_skips_the_autotune_motor_and_saves_the_other(runs, save):
    runs('01_x', 'x', A)
    runs('02_y', 'y', B)
    mk = save('MOTOR=AB', mk=FakeMoonraker({'printer.cfg': CFG_ABE}, settings=AUTOTUNE))
    assert changed(mk) == {'stepper_y'}
    # alone, the refusal is the failure: the display says what to change
    from chopper_autotune.collect import failure_display
    with pytest.raises(SystemExit, match=r'^motor A: not saving \[tmc2209 stepper_x\]: autotune resets') \
            as refused:
        save('MOTOR=A', mk=FakeMoonraker({'printer.cfg': CFG_ABE}, settings=AUTOTUNE))
    assert 'the log says what to do' in failure_display('save FAILED: %s' % refused.value.code)


def test_save_skips_a_run_measured_under_autotune(runs, save):
    # its CoolStep lowered the current under load: the optimum was found at another current
    runs('01_x', 'x', A, autotune='auto')
    extruder_run(E, autotune='silent')
    mk = FakeMoonraker({'printer.cfg': CFG_ABE})
    with pytest.raises(SystemExit, match=r'not saving \[tmc2209 stepper_x\]: measured under autotune'):
        save('MOTOR=AE', mk=mk)
    assert mk.uploads == [] and 'RESTART' not in mk.scripts
    assert len(console(mk)) == 1
    assert console(mk)[0].startswith('motor E: not saving [tmc2209 extruder]: measured under autotune')
    # the other motor still saves
    runs('02_y', 'y', B)
    mk = save('MOTOR=AB')
    assert changed(mk) == {'stepper_y'}
    assert console(mk)[0].startswith('motor A: not saving [tmc2209 stepper_x]: measured under autotune')


def test_save_skips_an_extruder_winner_klipper_would_not_load(runs, save):
    # a TMC2660 winner stored before the hysteresis limit: it must not block the motors
    runs('01_x', 'x', A)
    stale = tmc.Chopper(2, 4, 7, 10)
    extruder_run(stale, driver='2660')
    files = {'printer.cfg': CFG_ABE.replace('tmc2209 extruder', 'tmc2660 extruder')}
    mk = save(mk=FakeMoonraker(files))
    assert 'driver_TOFF: 8' in mk.files['printer.cfg'] and 'driver_TOFF: 4' not in mk.files['printer.cfg']
    assert console(mk)[2].startswith('motor E: refusing to save %s to [tmc2660 extruder]' % stale.label())
    with pytest.raises(SystemExit, match='Klipper refuses'):
        save('MOTOR=E', mk=FakeMoonraker(files))


def test_a_rail_measured_under_autotune_names_each_of_its_sections():
    mk = FakeMoonraker({'printer.cfg': RAIL_CFG}, settings=RAIL_SETTINGS)
    with pytest.raises(SystemExit, match=re.escape(
            'not saving [tmc2209 stepper_x] and [tmc2209 stepper_x1]: measured under autotune')):
        run_save(mk, [(dict(RAIL, autotune='performance'), tmc.Chopper(0, 8, 7, 5))])
    assert mk.uploads == [] and mk.scripts == []


def test_the_advice_for_a_rail_names_each_driver_with_the_thresholds_of_its_own():
    from chopper_autotune.collect import autotune_advice, autotune_refusal
    settings = {'autotune_tmc stepper_x': {'sg4_thrs': 80}, 'autotune_tmc stepper_x1': {'sg4_thrs': 60}}
    advice = autotune_advice(settings, '2209', 'stepper_x', 'stepper_x1')
    assert ('1) in [tmc2209 stepper_x] set driver_SGTHRS: 80 and in [tmc2209 stepper_x1] set '
            'driver_SGTHRS: 60 (autotune sets these') in advice
    assert '2) remove [autotune_tmc stepper_x] and [autotune_tmc stepper_x1] and restart' in advice
    assert autotune_refusal('2209', 'stepper_x', 'stepper_x1', settings=settings).startswith(
        'not saving [tmc2209 stepper_x] and [tmc2209 stepper_x1]: autotune resets')
    assert 'remove [autotune_tmc stepper_x] and [autotune_tmc stepper_x1], restart' \
        in autotune_advice({}, '2208', 'stepper_x', 'stepper_x1')


CFG_RAIL_ABE = CFG_ABE + """
[tmc2209 stepper_x1]
uart_pin: PA4
driver_TOFF: 3
"""
RAIL_RUN = {'motion': 'rail', 'steppers': ['stepper_x', 'stepper_x1']}
RAIL_ON_X = {'stepper_x': {}, 'stepper_x1': {}, 'tmc2209 stepper_x': {}, 'tmc2209 stepper_x1': {}}


def rail_changed(mk) -> 'set[str]':
    """changed(), the twin of motor A too."""
    text = mk.files['printer.cfg']
    twin = re.search(r'driver_TOFF: (\d+)', text.split('[tmc2209 stepper_x1]')[1]).group(1)
    return changed(mk) | ({'stepper_x1'} if twin != '3' else set())


def test_save_writes_a_rail_into_every_section_of_it_and_the_line_names_them(runs, save):
    # AWD on X (#129): the registers go to both drivers of motor A, and the console says so
    runs('01_x', 'x', A, **RAIL_RUN)
    runs('02_y', 'y', B)
    line = ('motor A: %s ' + A.label() + ' into [tmc2209 stepper_x] and [tmc2209 stepper_x1] '
            'from run 01_x')
    dry = save('MOTOR=A', 'DRY_RUN=1', mk=FakeMoonraker({'printer.cfg': CFG_RAIL_ABE},
                                                        settings=RAIL_ON_X))
    assert console(dry) == [line % 'would save', 'DRY_RUN=1: nothing written, Klipper keeps running']
    assert dry.uploads == [] and 'RESTART' not in dry.scripts
    mk = save('MOTOR=A', mk=FakeMoonraker({'printer.cfg': CFG_RAIL_ABE}, settings=RAIL_ON_X))
    assert rail_changed(mk) == {'stepper_x', 'stepper_x1'}
    assert console(mk) == [line % 'saving'] and mk.scripts[-1] == 'RESTART'


def test_a_rail_that_kept_its_shared_registers_is_not_rewritten(runs, save):
    own = tmc.Chopper(0, 2, 2, 12)
    runs('01_x', 'x', own, kept=True, **RAIL_RUN)
    holding = {'driver_' + field: value for field, value in own.fields().items()}
    settings = dict(RAIL_ON_X, **{'tmc2209 stepper_x': holding, 'tmc2209 stepper_x1': holding})
    mk = save('MOTOR=A', mk=FakeMoonraker({'printer.cfg': CFG_RAIL_ABE}, settings=settings))
    assert mk.uploads == [] and 'RESTART' not in mk.scripts
    assert console(mk)[0] == ('motor A: your registers %s stay in [tmc2209 stepper_x] and '
                              '[tmc2209 stepper_x1]. In run 01_x, neither the winner nor Klipper '
                              'defaults was quieter than them in all 4 rounds' % own.label())
    # the twin on other registers since: the save puts the pair on the kept ones again
    settings['tmc2209 stepper_x1'] = {'driver_toff': 3}
    mk = save('MOTOR=A', mk=FakeMoonraker({'printer.cfg': CFG_RAIL_ABE}, settings=settings))
    assert rail_changed(mk) == {'stepper_x', 'stepper_x1'}


@pytest.mark.parametrize('run, settings, refusal', [
    # klipper_tmc_autotune came onto the twin after the run
    (RAIL_RUN, dict(RAIL_ON_X, **{'autotune_tmc stepper_x1': {'tuning_goal': 'performance'}}),
     'motor A: not saving [tmc2209 stepper_x1]: autotune resets'),
    # the twin got another driver model since
    (RAIL_RUN, {'stepper_x': {}, 'stepper_x1': {}, 'tmc2209 stepper_x': {}, 'tmc5160 stepper_x1': {}},
     'motor A: tune motor A again: its dataset measured TMC2209 drivers, the config gives '
     'stepper_x1 TMC5160 now'),
    # the run measured the pair, the config drives the axis with stepper_x alone now
    (RAIL_RUN, {'stepper_x': {}, 'tmc2209 stepper_x': {}},
     'motor A: tune motor A again: its dataset measured stepper_x, stepper_x1, the config '
     'drives the axis with stepper_x alone now'),
    (dict(RAIL_RUN, autotune='auto'), RAIL_ON_X,
     'motor A: not saving [tmc2209 stepper_x] and [tmc2209 stepper_x1]: measured under autotune'),
])
def test_save_skips_a_rail_it_may_not_write_whole_and_saves_the_rest(runs, save, run, settings,
                                                                     refusal):
    runs('01_x', 'x', A, **run)
    runs('02_y', 'y', B)
    mk = save('MOTOR=AB', mk=FakeMoonraker({'printer.cfg': CFG_RAIL_ABE}, settings=settings))
    assert rail_changed(mk) == {'stepper_y'}
    assert console(mk)[0].startswith(refusal)
    with pytest.raises(SystemExit, match='^' + re.escape(refusal)):
        save('MOTOR=A', mk=FakeMoonraker({'printer.cfg': CFG_RAIL_ABE}, settings=settings))
