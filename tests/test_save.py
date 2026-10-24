import json
import os
import re
import time
from datetime import datetime

import pytest

from chopper_autotune import tmc
from chopper_autotune.analyze import LAST_SAVE, register_options, run_save, updated_options

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


def test_updated_options_replaces_and_inserts():
    out = updated_options(CFG, 'tmc2209 stepper_x', register_options(FIELDS))
    x_section = out.split('[tmc2209 stepper_y]')[0]
    assert 'driver_TBL: 0\ndriver_TOFF: 8\ndriver_HSTRT: 7\ndriver_HEND: 5\n' in x_section
    assert '# driver_TBL: 0' in x_section          # commented history is kept
    assert x_section.count('driver_TOFF') == 1     # active line replaced, not duplicated
    assert 'driver_hend: 13' not in x_section      # replaced case-insensitively
    assert 'run_current: 1.8' in x_section
    # the other section is untouched
    assert 'driver_TOFF: 5' in out.split('[tmc2209 stepper_y]')[1]


def test_updated_options_errors():
    with pytest.raises(SystemExit, match='not found'):
        updated_options(CFG, 'tmc2209 stepper_z', register_options(FIELDS))
    doubled = CFG + '\n[tmc2209 stepper_x]\nuart_pin: PC10\n'
    with pytest.raises(SystemExit, match='2 times'):
        updated_options(doubled, 'tmc2209 stepper_x', register_options(FIELDS))


class FakeMoonraker:
    def __init__(self, files, printing=False, settings=None, commands=None):
        self.files = dict(files)
        self.printing = printing
        self.config_settings = settings or {}
        self.commands = commands
        self.uploads = []
        self.deletes = []
        self.scripts = []

    def settings(self):
        return self.config_settings

    def accepted_commands(self):
        return self.commands

    def is_printing(self):
        return self.printing

    def list_config_files(self):
        return list(self.files)

    def download_config(self, name, missing_ok=False):
        return self.files.get(name) if missing_ok else self.files[name]

    def upload_config(self, name, content):
        self.uploads.append(name)
        self.files[name] = content

    def delete_config(self, name):
        self.deletes.append(name)
        del self.files[name]

    def gcode(self, script):
        self.scripts.append(script)

    def set_tmc_fields(self, stepper, fields):             # as Moonraker sends it
        self.gcode(tmc.set_fields_script(stepper, fields))


def test_run_save_backs_up_edits_and_restarts(capsys):
    mk = FakeMoonraker({'printer.cfg': CFG, 'mainsail.cfg': '[respond]\n'})
    run_save(mk, [({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))])

    assert mk.uploads == ['chopper-autotune/backups/printer.cfg.bak', LAST_SAVE, 'printer.cfg']
    assert mk.files['chopper-autotune/backups/printer.cfg.bak'] == CFG
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
    assert mk.uploads == ['chopper-autotune/backups/printer.cfg.bak', LAST_SAVE, 'printer.cfg']
    assert 'driver_TOFF: 8' in mk.files['printer.cfg']
    # the dated backups are untouched
    assert mk.files['printer-20250922_211125.cfg'] == CFG


def test_run_save_ignores_its_own_backups():
    mk = FakeMoonraker({'printer.cfg': CFG})
    item = [({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))]
    run_save(mk, item)
    mk.uploads.clear()
    # the copy now sits in the config folder with the same section: must not confuse a re-save
    run_save(mk, item)
    assert mk.uploads == ['chopper-autotune/backups/printer.cfg.bak', LAST_SAVE, 'printer.cfg']


def test_run_save_uploads_all_backups_before_edits():
    files = {'printer.cfg': '[include extra.cfg]\n' + CFG,
             'extra.cfg': '[tmc2209 stepper_z]\nuart_pin: PC10\ndriver_TOFF: 4\n'}
    mk = FakeMoonraker(files)
    run_save(mk, [
        ({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5)),
        ({'driver': '2209', 'stepper': 'stepper_z'}, tmc.Chopper(1, 6, 5, 4)),
    ])
    assert mk.uploads[:3] == ['chopper-autotune/backups/printer.cfg.bak',
                              'chopper-autotune/backups/extra.cfg.bak', LAST_SAVE]
    assert set(mk.uploads[3:]) == {'printer.cfg', 'extra.cfg'}
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


def test_restore_defaults_rewrites_tuned_sections_only(monkeypatch):
    from types import SimpleNamespace

    import chopper_autotune.analyze as analyze
    mk = FakeMoonraker({'printer.cfg': CONFIG_WITH_TUNING})
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    analyze.run_restore_config(SimpleNamespace(defaults=True, backup=False, url=''))
    text = mk.files['printer.cfg']
    x = text[text.index('[tmc2209 stepper_x]'):text.index('[tmc2209 stepper_y]')]
    assert 'driver_TBL: 2' in x and 'driver_HEND: 0' in x    # stock registers written
    assert 'run_current: 1.0' in x                           # the current is untouched
    assert mk.files['chopper-autotune/backups/printer.cfg.bak']  # snapshot taken first
    assert mk.scripts == ['RESTART']


def test_restore_needs_exactly_one_mode(monkeypatch):
    from types import SimpleNamespace

    import pytest

    import chopper_autotune.analyze as analyze
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: FakeMoonraker({}))
    with pytest.raises(SystemExit, match='pick one'):
        analyze.run_restore_config(SimpleNamespace(defaults=False, backup=False, url=''))


def test_restore_resets_the_plan_marks(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import chopper_autotune.analyze as analyze
    monkeypatch.setattr(analyze, 'RESULTS_HOME', tmp_path)
    for name in ('belts.json', 'current.json', 'envelope.json', 'map.json',
                 'state.json', 'extruder.json'):
        (tmp_path / name).write_text('{}')
    mk = FakeMoonraker({'printer.cfg': CONFIG_WITH_TUNING})
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
    mk = FakeMoonraker({'printer.cfg': CONFIG_2240_TUNED})
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


def test_run_save_refuses_to_write_one_driver_of_a_pair():
    # AWD: stepper_x1 drives the same belt; saving to [tmc stepper_x] alone would
    # leave the twin on its old registers (#129)
    mk = FakeMoonraker({'printer.cfg': CFG}, settings={'stepper_x': {}, 'stepper_x1': {}})
    with pytest.raises(SystemExit, match='stepper_x1 share its axis'):
        run_save(mk, [({'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))])
    assert mk.uploads == [] and mk.scripts == []


def test_apply_refuses_to_set_one_driver_of_a_pair():
    from chopper_autotune.analyze import run_apply
    mk = FakeMoonraker({}, settings={'stepper_x': {}, 'stepper_x1': {}})
    with pytest.raises(SystemExit, match='stepper_x1 share its axis'):
        run_apply(mk, 'stepper_x', tmc.Chopper(0, 8, 7, 5))
    assert mk.scripts == []


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
    run_apply(mk, 'stepper_x', tmc.Chopper(2, 3, 5, 0))
    assert mk.scripts[0] == 'SET_STEPPER_ENABLE STEPPER=stepper_x ENABLE=1'
    assert mk.scripts[1].startswith('SET_TMC_FIELD')
    assert ('no enable pin of its own' in capsys.readouterr().out) is shared
    # APPLY runs detached: the warning reaches the console too, not just the log
    assert (mk.scripts[-1].startswith('M118 WARNING: stepper_x has no enable pin')) is shared


def test_apply_sends_its_warning_only_where_klipper_takes_m118(capsys):
    # without [respond] the M118 drew an 'Unknown command' line and a KlipperScreen error
    from chopper_autotune.analyze import run_apply
    mk = FakeMoonraker({}, settings={'stepper_x': {}}, commands={'M117', 'SET_TMC_FIELD'})
    run_apply(mk, 'stepper_x', tmc.Chopper(2, 3, 5, 0))
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
        run_apply(mk, 'stepper_x', tmc.Chopper(2, 3, 5, 0))
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
X, Y = {'driver': '2209', 'stepper': 'stepper_x'}, {'driver': '2209', 'stepper': 'stepper_y'}


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


def timeless(files: dict) -> dict:
    """The files of a save with the time left out of its record."""
    return dict(files, **{LAST_SAVE: json.loads(files[LAST_SAVE])['sections']})


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


@pytest.mark.parametrize('backup, record', [(False, False), (True, False), (True, True)])
def test_runs_from_before_the_last_restore_are_not_saved(runs, save, monkeypatch, backup, record):
    # the next save must not write the rolled-back registers again
    from types import SimpleNamespace

    from chopper_autotune import analyze
    old = runs('01_x', 'x', A, age=60)
    extruder_run(E, age=60)
    mk = FakeMoonraker({'printer.cfg': CFG_ABE, 'printer.chopper-backup.cfg': CFG_ABE})
    if record:
        run_save(mk, [(X, A)])
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
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
    # the record of each save carries its own time
    assert console(mk) == console(saved) and timeless(mk.files) == timeless(saved.files)
    assert changed(mk) == {'extruder'}


def test_save_skips_a_motor_with_a_twin_and_saves_the_rest(runs, save):
    # AWD on X only: motor A would reach one driver of its pair; B and the extruder still save
    runs('01_x', 'x', A)
    runs('02_y', 'y', B)
    extruder_run(E)
    mk = save(mk=FakeMoonraker({'printer.cfg': CFG_ABE}, settings={'stepper_x': {}, 'stepper_x1': {}}))
    assert changed(mk) == {'stepper_y', 'extruder'}
    assert console(mk)[0].startswith('motor A: refusing to write registers to stepper_x only: '
                                     'stepper_x1 share its axis')


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


BEFORE_SAVE = """[include extra.cfg]

[tmc2209 stepper_x]
driver_TBL: 1
driver_toff = 3  # by hand
uart_pin: PA1
run_current: 1.20

[tmc2209 stepper_y]
uart_pin: PA2
run_current: 1.20
"""
EXTRA_CFG = """[tmc2209 extruder]
uart_pin: PA3
run_current: 0.65
"""
# as SAVE_CONFIG appends it after SHAPER_CALIBRATE and PROBE_CALIBRATE: inside the span
# of the last section of printer.cfg
AUTOSAVE = """
#*# <---------------------- SAVE_CONFIG ---------------------->
#*# DO NOT EDIT THIS BLOCK OR BELOW. The contents are auto-generated.
#*#
#*# [input_shaper]
#*# shaper_type_x = mzv
#*# shaper_freq_x = 52.4
#*#
#*# [probe]
#*# z_offset = 1.725
"""


def read(text: str) -> dict:
    """The options of each section as Klipper reads them: '#' starts a comment anywhere."""
    import configparser
    fileconfig = configparser.RawConfigParser(strict=False, inline_comment_prefixes=(';', '#'))
    fileconfig.read_string('\n'.join(line.split('#', 1)[0] for line in text.split('\n')))
    return {section: dict(fileconfig.items(section)) for section in fileconfig.sections()}


def saved_registers() -> FakeMoonraker:
    """A printer whose last save wrote registers of motors A and B into printer.cfg."""
    mk = FakeMoonraker({'printer.cfg': BEFORE_SAVE, 'extra.cfg': EXTRA_CFG})
    run_save(mk, [(X, A), (Y, B)])
    mk.scripts.clear()
    return mk


def test_backup_puts_back_only_the_lines_the_last_save_wrote(monkeypatch):
    mk = saved_registers()
    # after the save: SAVE_CONFIG appends its block, a hand lowers a current the save did not write
    by_hand = ('run_current: 1.20\n', 'run_current: 1.10\n', 1)
    mk.files['printer.cfg'] = mk.files['printer.cfg'].replace(*by_hand) + AUTOSAVE
    backup(mk, monkeypatch)
    assert mk.files['printer.cfg'] == BEFORE_SAVE.replace(*by_hand) + AUTOSAVE
    assert mk.files['extra.cfg'] == EXTRA_CFG
    assert mk.scripts[-1] == 'RESTART'


def test_backup_undoes_the_last_save_alone(monkeypatch):
    # save 1 wrote the extruder into extra.cfg, save 2 the run currents into printer.cfg:
    # the copies of both files are there, the rollback undoes save 2 only
    from chopper_autotune.analyze import run_save_currents
    mk = FakeMoonraker({'printer.cfg': BEFORE_SAVE, 'extra.cfg': EXTRA_CFG})
    run_save(mk, [], {'driver': '2209', 'fields': E.fields()})
    first = dict(mk.files)
    run_save_currents(mk, [('2209', 'stepper_x', 0.8), ('2209', 'stepper_y', 0.9)])
    backup(mk, monkeypatch)
    assert read(mk.files['printer.cfg']) == read(first['printer.cfg'])
    assert mk.files['extra.cfg'] == first['extra.cfg']
    assert read(first['extra.cfg'])['tmc2209 extruder']['driver_toff'] == '7'


def test_a_second_backup_finds_nothing_to_put_back(monkeypatch):
    from chopper_autotune import analyze
    mk = saved_registers()
    backup(mk, monkeypatch)
    # measured after the rollback, against the registers it put back
    envelope = analyze.RESULTS_HOME / 'envelope.json'
    envelope.write_text('{}')
    rolled_back, mk.uploads, mk.scripts = dict(mk.files), [], []
    backup(mk, monkeypatch)
    assert mk.files == rolled_back and mk.uploads == [] and 'RESTART' not in mk.scripts
    assert console(mk) == ['Nothing to put back from the last save, made %s: the lines it wrote '
                           'already have the values they had before it. Klipper keeps running' % when(mk)]
    assert envelope.exists()


def test_a_backup_after_a_hand_put_the_lines_back_still_rolls_the_save_back(runs, save, monkeypatch):
    # the next CHOPPER_SAVE would write the rolled-back registers again
    runs('01_x', 'x', A, age=60)
    mk = save()
    mk.files['printer.cfg'], mk.scripts = CFG_ABE, []
    backup(mk, monkeypatch)
    assert console(mk) == ['Nothing to put back from the last save, made %s: the lines it wrote '
                           'already have the values they had before it. Klipper keeps running' % when(mk)]
    mk.scripts = []
    save(mk=mk)
    assert console(mk)[0].startswith('motor A: not saving run 01_x: it is older than the last '
                                     'CHOPPER_RESTORE')
    assert mk.files['printer.cfg'] == CFG_ABE


def test_a_backup_after_defaults_puts_back_the_registers_it_replaced(monkeypatch):
    from types import SimpleNamespace

    from chopper_autotune import analyze
    mk = saved_registers()
    tuned = read(mk.files['printer.cfg'])
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    analyze.run_restore_config(SimpleNamespace(defaults=True, backup=False, url=''))
    assert read(mk.files['printer.cfg'])['tmc2209 stepper_x']['driver_tbl'] == '2'
    mk.files['printer.cfg'] += AUTOSAVE
    backup(mk, monkeypatch)
    assert read(mk.files['printer.cfg']) == tuned
    assert mk.files['printer.cfg'].endswith(AUTOSAVE)


def test_an_extruder_tune_that_saves_is_rolled_back_too(monkeypatch):
    # CHOPPER_EXTRUDER SAVE=1 writes through a call of its own, not through CHOPPER_SAVE's
    from types import SimpleNamespace

    from chopper_autotune import extruder, moonraker
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    mk = FakeMoonraker({'printer.cfg': BEFORE_SAVE, 'extra.cfg': EXTRA_CFG})
    monkeypatch.setattr(moonraker, 'Moonraker', lambda url: mk)
    kl = SimpleNamespace(settings=lambda: {'tmc2209 extruder': {}, 'extruder': {}},
                         gcode=lambda script: None, subscribe_accel=lambda chip: None)
    for name, stub in (('refuse_if_printing', lambda kl: None),
                       ('detect_hardware', lambda kl, axis: SimpleNamespace(accel_chip='adxl345',
                                                                           display=False)),
                       ('resolve_extruder_stealth', lambda kl, driver, stealth: None),
                       ('measure', lambda hw, speed: (100.0, 0))):
        monkeypatch.setattr(extruder, name, stub)
    parser = build_parser()
    extruder.extruder_tune(kl, parser.parse_args(_gcode_args(['extruder', 'SPEED=5', 'YES=1', 'SAVE=1'],
                                                             boolean_flags(parser))))
    assert {'driver_tbl', 'driver_toff', 'driver_hstrt', 'driver_hend'} \
        <= set(read(mk.files['extra.cfg'])['tmc2209 extruder'])
    backup(mk, monkeypatch)
    assert mk.files['extra.cfg'] == EXTRA_CFG and mk.files['printer.cfg'] == BEFORE_SAVE


def when(mk) -> str:
    return datetime.fromtimestamp(json.loads(mk.files[LAST_SAVE])['time']).strftime('%Y-%m-%d %H:%M')


@pytest.mark.parametrize('edit, warning', [
    # a hand or another tool rewrote a line of the save
    (('driver_TOFF: 8\n', 'driver_TOFF: 5\n'),
     '[tmc2209 stepper_x] driver_TOFF changed after the save: the save wrote 8, the config has 5. '
     'The rollback still puts back 3'),
    (('driver_TOFF: 8\n', ''),
     '[tmc2209 stepper_x] driver_TOFF changed after the save: the save wrote 8, the config has '
     'no such line. The rollback still puts back 3'),
    (('driver_HEND: 2\n', 'driver_HEND: 9\n'),
     '[tmc2209 stepper_y] driver_HEND changed after the save: the save wrote 2, the config has 9. '
     "The rollback still puts back Klipper's default"),
])
def test_a_line_changed_after_the_save_goes_back_with_a_warning(monkeypatch, edit, warning):
    mk = saved_registers()
    mk.files['printer.cfg'] = mk.files['printer.cfg'].replace(*edit)
    backup(mk, monkeypatch)
    assert [line for line in console(mk) if 'changed after the save' in line] == [warning]
    config = read(mk.files['printer.cfg'])
    assert config['tmc2209 stepper_x']['driver_toff'] == '3'
    assert config['tmc2209 stepper_x']['driver_tbl'] == '1'
    assert not any(option.startswith('driver_') for option in config['tmc2209 stepper_y'])
    assert mk.scripts[-1] == 'RESTART'


@pytest.mark.parametrize('comment', ['# stock', '; stock'])
def test_a_line_put_back_by_hand_stays_as_the_hand_wrote_it(monkeypatch, comment):
    # Klipper reads the value without the comment: nothing changed after the save, nothing to say
    mk = saved_registers()
    by_hand = 'driver_TOFF: 3 %s\n' % comment
    mk.files['printer.cfg'] = mk.files['printer.cfg'].replace('driver_TOFF: 8\n', by_hand)
    backup(mk, monkeypatch)
    assert not any('changed after the save' in line for line in console(mk))
    assert by_hand in mk.files['printer.cfg']


def test_the_console_names_the_lines_before_klipper_restarts(monkeypatch):
    mk = saved_registers()
    backup(mk, monkeypatch)
    assert console(mk) == [
        'Rolling back the last save, made %s:' % when(mk),
        "printer.cfg [tmc2209 stepper_x] gets back driver_TBL: 1, driver_TOFF: 3 and Klipper's "
        'defaults for driver_HSTRT, driver_HEND',
        "printer.cfg [tmc2209 stepper_y] gets back Klipper's defaults for driver_TBL, driver_TOFF, "
        'driver_HSTRT, driver_HEND',
        'The rest of the config stays as it is, including what SAVE_CONFIG wrote. Klipper restarts now']
    assert [script.startswith('RESPOND') for script in mk.scripts] == [True, False]
    assert mk.scripts[-1] == 'RESTART'


def test_a_section_gone_since_the_save_is_named_and_the_rest_goes_back(monkeypatch):
    mk = saved_registers()
    text = mk.files['printer.cfg']
    mk.files['printer.cfg'] = text[:text.index('[tmc2209 stepper_y]')]
    backup(mk, monkeypatch)
    assert '[tmc2209 stepper_y] is not in the config any more: nothing to put back there' \
        in console(mk)
    assert read(mk.files['printer.cfg'])['tmc2209 stepper_x']['driver_toff'] == '3'


def test_with_every_section_of_the_save_gone_nothing_goes_back(monkeypatch):
    # the motors got another driver: no line of the save is left to hold its old value
    mk = saved_registers()
    mk.files['printer.cfg'] = mk.files['printer.cfg'].replace('[tmc2209 ', '[tmc2240 ')
    files = dict(mk.files)
    backup(mk, monkeypatch)
    assert console(mk) == [
        '[tmc2209 stepper_x] is not in the config any more: nothing to put back there',
        '[tmc2209 stepper_y] is not in the config any more: nothing to put back there',
        'Nothing to put back from the last save, made %s. Klipper keeps running' % when(mk)]
    assert mk.files == files and 'RESTART' not in mk.scripts


def test_a_file_that_ends_without_a_newline_gets_its_lines_back(monkeypatch):
    # as a web editor may leave it: the old last line goes back under the section header
    extra = EXTRA_CFG + 'driver_TOFF: 3'
    mk = FakeMoonraker({'printer.cfg': BEFORE_SAVE, 'extra.cfg': extra})
    run_save(mk, [], {'driver': '2209', 'fields': E.fields()})
    backup(mk, monkeypatch)
    assert read(mk.files['extra.cfg']) == read(extra)


# a config split into folders, as [include config/*.cfg] reads it
STEPPERS = """[tmc2209 stepper_x]
uart_pin: PA1
run_current: 1.20
driver_TOFF: 3

[tmc2209 stepper_y]
uart_pin: PA2
run_current: 1.20
"""
# what an older version left beside the file before the save that wrote driver_TOFF: 3
OLD_COPY = STEPPERS.replace('driver_TOFF: 3', 'driver_TOFF: 5')
MOVE = ('This command moves the backup copies that older versions of the tool left next to your '
        'files into chopper-autotune/backups: %s. Their new names end in .bak, so Klipper does not '
        'read them through an [include] of .cfg files')
RESTART = '. Klipper stops using the old values when it restarts'
# the line of a config split into folders with OLD_COPY beside config/steppers.cfg
STEPPERS_COPY = ('Klipper reads config/steppers.chopper-backup.cfg through an [include]. In '
                 '[tmc2209 stepper_x], Klipper uses old values from it instead of the values in '
                 'your config. '
                 + MOVE % 'config/steppers.chopper-backup.cfg' + RESTART)


def split_config(**files) -> FakeMoonraker:
    """printer.cfg with [include config/*.cfg], config/steppers.cfg = STEPPERS and OLD_COPY
    beside it, unless files say otherwise."""
    return FakeMoonraker(dict({'printer.cfg': '[include config/*.cfg]\n', 'config/steppers.cfg': STEPPERS,
                               'config/steppers.chopper-backup.cfg': OLD_COPY}, **files))


def test_a_save_copies_each_file_into_the_backups_folder():
    # no [include ...*.cfg] takes these names: Klipper never reads the old values over the saved ones
    mk = FakeMoonraker({'printer.cfg': '[include config/*.cfg]\n[include extra.cfg]\n',
                        'config/steppers.cfg': STEPPERS, 'extra.cfg': EXTRA_CFG})
    run_save(mk, [(X, A)], {'driver': '2209', 'fields': E.fields()})
    assert mk.uploads[:2] == ['chopper-autotune/backups/config/steppers.cfg.bak',
                              'chopper-autotune/backups/extra.cfg.bak']
    assert mk.files['chopper-autotune/backups/config/steppers.cfg.bak'] == STEPPERS
    assert mk.files['chopper-autotune/backups/extra.cfg.bak'] == EXTRA_CFG


def test_a_save_moves_the_old_copies_before_it_writes():
    # [include config/*.cfg] takes config/steppers.chopper-backup.cfg too, after the file it is
    # a copy of: Klipper merges the two sections, the values of the copy win
    mk = FakeMoonraker({'printer.cfg': '[include config/*.cfg]\n', 'config/steppers.cfg': STEPPERS,
                        'config/steppers.chopper-backup.cfg': OLD_COPY,
                        'printer.chopper-backup.cfg': 'pristine'})
    run_save(mk, [(X, A)], lines=['motor A: saving'])
    assert console(mk) == [
        'Klipper reads config/steppers.chopper-backup.cfg through an [include]. In '
        '[tmc2209 stepper_x], Klipper uses old values from it instead of the values in your '
        'config. '
        + MOVE % 'config/steppers.chopper-backup.cfg, printer.chopper-backup.cfg' + RESTART,
        'motor A: saving']
    assert mk.deletes == ['config/steppers.chopper-backup.cfg', 'printer.chopper-backup.cfg']
    assert mk.uploads == ['chopper-autotune/backups/config/steppers.cfg.bak',
                          'chopper-autotune/backups/printer.cfg.bak',
                          'chopper-autotune/backups/config/steppers.cfg.bak', LAST_SAVE,
                          'config/steppers.cfg']
    assert mk.files['chopper-autotune/backups/printer.cfg.bak'] == 'pristine'
    assert mk.files['chopper-autotune/backups/config/steppers.cfg.bak'] == STEPPERS
    assert not any(name.endswith('.chopper-backup.cfg') for name in mk.files)
    assert read(mk.files['config/steppers.cfg'])['tmc2209 stepper_x']['driver_toff'] == '8'


def restore(mk, monkeypatch, mode):
    """CHOPPER_RESTORE <mode>=1 against mk."""
    from chopper_autotune import analyze
    from chopper_autotune.cli import _gcode_args, boolean_flags, build_parser
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: mk)
    parser = build_parser()
    analyze.run_restore_config(parser.parse_args(_gcode_args(['restore', mode + '=1'],
                                                             boolean_flags(parser))))


def backup(mk, monkeypatch):
    """CHOPPER_RESTORE BACKUP=1 against mk."""
    restore(mk, monkeypatch, 'BACKUP')


@pytest.mark.parametrize('mode, after', [
    ('BACKUP', 'The last save left no record of the lines it changed, so whole files go back: '
               'config/steppers.cfg from chopper-autotune/backups/config/steppers.cfg.bak'),
    ('DEFAULTS', None)])
def test_a_restore_moves_the_old_copies_before_it_writes(monkeypatch, mode, after):
    mk = split_config()
    restore(mk, monkeypatch, mode)
    assert console(mk)[0] == STEPPERS_COPY
    assert console(mk)[1:2] == ([after] if after else [])
    assert mk.uploads[0] == 'chopper-autotune/backups/config/steppers.cfg.bak'
    assert mk.deletes == ['config/steppers.chopper-backup.cfg']
    assert mk.scripts[-1] == 'RESTART'


@pytest.mark.parametrize('save', [
    lambda mk, monkeypatch: run_save(mk, [(X, A)]),
    lambda mk, monkeypatch: backup(mk, monkeypatch)])
def test_the_console_hears_of_the_old_copies_before_the_first_write(monkeypatch, save):
    from chopper_autotune.moonraker import MoonrakerError
    mk = split_config()

    def fail(name, content):
        raise MoonrakerError('cannot upload %s' % name)
    mk.upload_config = fail
    with pytest.raises(MoonrakerError):
        save(mk, monkeypatch)
    assert console(mk) == [STEPPERS_COPY]
    assert mk.files['config/steppers.chopper-backup.cfg'] == OLD_COPY


def test_with_nothing_to_save_the_old_copies_move_all_the_same(save):
    # else Klipper reads the old registers until a save that writes something
    mk = save(mk=split_config())
    assert console(mk) == [STEPPERS_COPY, 'motor A: no tuning run to save',
                           'motor B: no tuning run to save', 'motor E: no CHOPPER_EXTRUDER run to save',
                           'Nothing to save, Klipper keeps running']
    assert mk.deletes == ['config/steppers.chopper-backup.cfg'] and 'RESTART' not in mk.scripts


def test_a_dry_run_names_the_old_copies_and_moves_nothing(save):
    mk = save('DRY_RUN=1', mk=split_config())
    assert console(mk)[0] == STEPPERS_COPY.replace(' moves ', ' would move ').replace(RESTART, '')
    assert mk.uploads == [] and mk.deletes == []


def test_the_old_copies_stay_while_printing(save):
    mk = split_config()
    mk.printing = True
    with pytest.raises(SystemExit, match='busy printing'):
        save(mk=mk)
    assert console(mk) == [] and mk.uploads == [] and mk.deletes == []


def test_klipper_settings_are_weighed_as_without_the_old_copies():
    # an option and a section that only the copy has go, a value the copy gave goes back
    from chopper_autotune.analyze import old_copies, settings_without
    mk = split_config(**{'config/steppers.chopper-backup.cfg': OLD_COPY.replace(
        'driver_TOFF: 5', 'driver_TOFF: 5\ndriver_HEND: 9') + '\n[tmc2209 stepper_z]\nuart_pin: PA4\n'})
    klipper = {'tmc2209 stepper_x': {'uart_pin': 'PA1', 'driver_toff': 5, 'driver_hend': 9},
               'tmc2209 stepper_y': {'uart_pin': 'PA2'}, 'tmc2209 stepper_z': {'uart_pin': 'PA4'}}
    assert settings_without(klipper, old_copies(mk)[2]) == {
        'tmc2209 stepper_x': {'uart_pin': 'PA1', 'driver_toff': '3'},
        'tmc2209 stepper_y': {'uart_pin': 'PA2'}}


def test_defaults_moves_the_old_copies_with_nothing_else_to_write(monkeypatch):
    # the config has no driver_* line left, and Klipper reads the copy's
    mk = split_config(**{'config/steppers.cfg': STEPPERS.replace('driver_TOFF: 3\n', '')})
    with pytest.raises(SystemExit, match='no tuned TMC sections found'):
        restore(mk, monkeypatch, 'DEFAULTS')
    assert console(mk) == [STEPPERS_COPY]
    assert mk.deletes == ['config/steppers.chopper-backup.cfg']


def test_a_save_to_a_section_only_an_old_copy_holds_names_the_copy_before_it_refuses():
    mk = split_config(**{'config/steppers.cfg': STEPPERS.split('\n\n')[1]})
    with pytest.raises(SystemExit, match=r'section \[tmc2209 stepper_x\] not found in any config file'):
        run_save(mk, [(X, A)])
    assert console(mk) == [STEPPERS_COPY]
    assert mk.deletes == ['config/steppers.chopper-backup.cfg']


def test_registers_a_run_kept_from_an_old_copy_go_into_the_config(runs, save):
    # Klipper ran the motor on the registers of the copy, and the run kept them: once the
    # copy moves, the lines of the config would run instead
    own = tmc.Chopper(0, 2, 2, 12)
    runs('01_x', 'x', own, kept=True)
    mk = split_config(**{'config/steppers.chopper-backup.cfg': updated_options(
        STEPPERS, 'tmc2209 stepper_x', register_options(own.fields()))})
    mk.config_settings = {'tmc2209 stepper_x': {'driver_' + field: value
                                                for field, value in own.fields().items()}}
    save('MOTOR=A', mk=mk)
    assert console(mk) == [STEPPERS_COPY, 'motor A: saving %s from run 01_x' % own.label()]
    assert read(mk.files['config/steppers.cfg'])['tmc2209 stepper_x']['driver_toff'] == '2'


# the first sentence of the line about OLD_COPY beside config/steppers.cfg
OLD_STEPPER_X = ('Klipper reads config/steppers.chopper-backup.cfg through an [include]. In '
                 '[tmc2209 stepper_x], Klipper uses old values from it instead of the values in '
                 'your config')
NONE_CHANGES = ('Klipper reads config/steppers.chopper-backup.cfg through an [include], but this '
                'changes none of the values Klipper uses')


@pytest.mark.parametrize('files, line', [
    # the copy differs in one section: Klipper reads that one from it
    ({'config/steppers.chopper-backup.cfg': OLD_COPY, 'config/steppers.cfg': STEPPERS}, OLD_STEPPER_X),
    # listed in another order: Klipper reads the files a pattern takes sorted by name
    ({'config/steppers.cfg': STEPPERS, 'config/steppers.chopper-backup.cfg': OLD_COPY,
      'config/extruder.chopper-backup.cfg': EXTRA_CFG.replace('0.65', '0.50'),
      'config/extruder.cfg': EXTRA_CFG},
     'Klipper reads config/extruder.chopper-backup.cfg, config/steppers.chopper-backup.cfg through '
     'an [include]. In [tmc2209 extruder], [tmc2209 stepper_x], Klipper uses old values from them '
     'instead of the values in your config'),
    # a copy Klipper reads that changes nothing is not named with the one that does
    ({'config/steppers.cfg': STEPPERS, 'config/steppers.chopper-backup.cfg': OLD_COPY,
      'config/extruder.chopper-backup.cfg': EXTRA_CFG, 'config/extruder.cfg': EXTRA_CFG},
     OLD_STEPPER_X),
    # two copies give one option: Klipper takes it from the one it reads last
    ({'config/steppers.chopper-backup.cfg': OLD_COPY, 'config/steppers.cfg': STEPPERS,
      'late/steppers.chopper-backup.cfg': OLD_COPY,
      'printer.cfg': '[include config/*.cfg]\n[include late/*.cfg]\n'},
     'Klipper reads late/steppers.chopper-backup.cfg through an [include]. In [tmc2209 stepper_x], '
     'Klipper uses old values from it instead of the values in your config'),
    # a section the config no longer has comes back from the copy, with options or without
    ({'config/steppers.chopper-backup.cfg': STEPPERS + '\n[tmc2209 stepper_z]\nuart_pin: PA4\n',
      'config/steppers.cfg': STEPPERS},
     'Klipper reads config/steppers.chopper-backup.cfg through an [include]. In '
     '[tmc2209 stepper_z], Klipper uses old values from it instead of the values in your config'),
    ({'config/steppers.chopper-backup.cfg': STEPPERS + '\n[exclude_object]\n',
      'config/steppers.cfg': STEPPERS},
     'Klipper reads config/steppers.chopper-backup.cfg through an [include]. In [exclude_object], '
     'Klipper uses old values from it instead of the values in your config'),
    # a pattern is taken from the folder of the file the [include] is in
    ({'printer.cfg': '[include config/main.cfg]\n', 'config/main.cfg': '[include parts/*.cfg]\n',
      'config/parts/steppers.cfg': STEPPERS, 'config/parts/steppers.chopper-backup.cfg': OLD_COPY},
     'Klipper reads config/parts/steppers.chopper-backup.cfg through an [include]. In '
     '[tmc2209 stepper_x], Klipper uses old values from it instead of the values in your config'),
    ({'config/steppers.chopper-backup.cfg': STEPPERS, 'config/steppers.cfg': STEPPERS}, NONE_CHANGES),
    # read later, the file itself puts its values back over the copy, which still holds others
    ({'config/steppers.chopper-backup.cfg': OLD_COPY, 'config/steppers.cfg': STEPPERS,
      'printer.cfg': '[include config/*.cfg]\n[include config/steppers.cfg]\n'}, NONE_CHANGES),
    # a section of the file with the [include], above it, loses to the copy
    ({'config/steppers.chopper-backup.cfg': STEPPERS.replace('uart_pin: PA2\nrun_current: 1.20',
                                                             'uart_pin: PA2\nrun_current: 1.00'),
      'config/steppers.cfg': STEPPERS,
      'printer.cfg': '[tmc2209 stepper_y]\nrun_current: 1.20\n\n[include config/*.cfg]\n'},
     'Klipper reads config/steppers.chopper-backup.cfg through an [include]. In '
     '[tmc2209 stepper_y], Klipper uses old values from it instead of the values in your config'),
    # one below it wins over the copy
    ({'config/steppers.chopper-backup.cfg': OLD_COPY.replace('run_current: 1.20', 'run_current: 1.00'),
      'config/steppers.cfg': STEPPERS.replace('driver_TOFF: 3', 'driver_TOFF: 5'),
      'printer.cfg': '[include config/*.cfg]\n\n[tmc2209 stepper_x]\nrun_current: 1.20\n'
                     '[tmc2209 stepper_y]\nrun_current: 1.20\n'}, NONE_CHANGES),
    ({'config/steppers.chopper-backup.cfg': OLD_COPY, 'config/steppers.cfg': STEPPERS,
      'printer.cfg': '[include config/steppers.cfg]\n'}, None),
])
def test_the_old_copies_line_names_the_sections_klipper_reads_from_them(files, line):
    from chopper_autotune.analyze import old_copies
    moves, lines, _ = old_copies(FakeMoonraker(dict({'printer.cfg': '[include config/*.cfg]\n'},
                                                    **files)))
    old = [name for name in files if name.endswith('.chopper-backup.cfg')]
    assert lines == [(line + '. ' if line else '') + MOVE % ', '.join(old)
                     + (RESTART if line and ' uses old values ' in line else '')]
    assert moves == {name: ('chopper-autotune/backups/'
                            + name.replace('.chopper-backup.cfg', '.cfg.bak'), files[name])
                     for name in old}


def test_without_old_copies_nothing_moves():
    from chopper_autotune.analyze import old_copies
    mk = FakeMoonraker({'printer.cfg': CFG, 'chopper-autotune/backups/printer.cfg.bak': CFG})
    assert old_copies(mk) == ({}, [], {})


WHOLE_FILES = ('The last save left no record of the lines it changed, so whole files go back: '
               'printer.cfg from chopper-autotune/backups/printer.cfg.bak, extra.cfg from '
               'chopper-autotune/backups/extra.cfg.bak')
LOST = ('This also undoes anything written to these files after the tool copied them, such as the '
        'input shaper, bed mesh and Z offset from SAVE_CONFIG. Klipper restarts now')


def test_a_save_without_a_record_goes_back_as_whole_files_with_a_warning(monkeypatch):
    # a save of an older version wrote no record
    mk = FakeMoonraker({'printer.cfg': BEFORE_SAVE + AUTOSAVE, 'extra.cfg': EXTRA_CFG,
                        'chopper-autotune/backups/printer.cfg.bak': BEFORE_SAVE,
                        'chopper-autotune/backups/extra.cfg.bak': 'pristine'})
    backup(mk, monkeypatch)
    assert mk.files['printer.cfg'] == BEFORE_SAVE and mk.files['extra.cfg'] == 'pristine'
    assert console(mk) == [WHOLE_FILES, LOST]
    assert [script.startswith('RESPOND') for script in mk.scripts] == [True, False]
    assert mk.scripts[-1] == 'RESTART'


@pytest.mark.parametrize('printer, extra', [
    ('printer.chopper-backup.cfg', 'extra.chopper-backup.cfg'),
    ('printer.chopper-backup.cfg', 'chopper-autotune/backups/extra.cfg.bak'),
    ('chopper-autotune/backups/printer.cfg.bak', 'extra.chopper-backup.cfg')])
def test_whole_files_go_back_from_the_copies_in_either_place(monkeypatch, printer, extra):
    # an older version keeps its copies beside the files
    mk = FakeMoonraker({'printer.cfg': BEFORE_SAVE + AUTOSAVE, 'extra.cfg': EXTRA_CFG,
                        printer: BEFORE_SAVE, extra: 'pristine'})
    backup(mk, monkeypatch)
    assert mk.files['printer.cfg'] == BEFORE_SAVE and mk.files['extra.cfg'] == 'pristine'
    assert sorted(console(mk)[-2].split(': ', 1)[1].split(', ')) \
        == sorted(WHOLE_FILES.split(': ', 1)[1].split(', '))
    assert console(mk)[-1] == LOST
    assert mk.scripts[-1] == 'RESTART'


@pytest.mark.parametrize('rolled_back', [False, True])
def test_a_save_of_an_older_version_after_a_recorded_one_goes_back_as_whole_files(monkeypatch,
                                                                                 rolled_back):
    # it left a copy beside the file and the record of the save before it alone. After a
    # rollback of that save, its copy can match the copy of that save byte for byte
    mk = saved_registers()
    if rolled_back:
        backup(mk, monkeypatch)
    copied = mk.files['printer.cfg']
    mk.files['printer.chopper-backup.cfg'] = copied
    mk.files['printer.cfg'] = updated_options(copied, 'tmc2209 stepper_x', register_options(B.fields()))
    mk.scripts = []
    backup(mk, monkeypatch)
    assert mk.files['printer.cfg'] == copied
    assert console(mk)[1].startswith('The last save left no record of the lines it changed, so '
                                     'whole files go back: printer.cfg from '
                                     'chopper-autotune/backups/printer.cfg.bak')


def saved_with_copies_beside_the_files() -> FakeMoonraker:
    """saved_registers() as an older version that writes the record leaves it: the copies
    beside their files, and the digest in the record taken over them there."""
    import hashlib
    mk = saved_registers()
    for copy in [name for name in mk.files if name.startswith('chopper-autotune/backups/')]:
        name = copy[len('chopper-autotune/backups/'):-len('.cfg.bak')] + '.chopper-backup.cfg'
        mk.files[name] = mk.files.pop(copy)
    digest = hashlib.sha256()
    for name in sorted(name for name in mk.files if name.endswith('.chopper-backup.cfg')):
        digest.update(('%s\0%s\0' % (name, mk.files[name])).encode())
    mk.files[LAST_SAVE] = json.dumps(dict(json.loads(mk.files[LAST_SAVE]), copies=digest.hexdigest()))
    return mk


@pytest.mark.parametrize('moved_by_a_save', [False, True])
def test_a_record_with_the_copies_beside_the_files_still_rolls_back_by_its_lines(
        save, monkeypatch, moved_by_a_save):
    # the copies move, the record stays the record of the last save: what SAVE_CONFIG
    # wrote after it stays too
    mk = saved_with_copies_beside_the_files()
    mk.files['printer.cfg'] += AUTOSAVE
    if moved_by_a_save:
        save(mk=mk)
    mk.scripts = []
    backup(mk, monkeypatch)
    assert mk.files['printer.cfg'] == BEFORE_SAVE + AUTOSAVE
    assert 'Rolling back the last save, made %s:' % when(mk) in console(mk)
    assert not any(name.endswith('.chopper-backup.cfg') for name in mk.files)
    mk.scripts = []
    backup(mk, monkeypatch)
    assert console(mk) == ['Nothing to put back from the last save, made %s: the lines it wrote '
                           'already have the values they had before it. Klipper keeps running' % when(mk)]


def test_a_save_that_fails_before_its_record_leaves_the_record_out(monkeypatch):
    # it wrote its copies: the record of the save before it is not of the last save
    from chopper_autotune.analyze import run_save_currents
    from chopper_autotune.moonraker import MoonrakerError
    mk = saved_registers()
    upload = mk.upload_config

    def fail_at_the_record(name, content):
        if name == LAST_SAVE:
            raise MoonrakerError('cannot upload %s' % name)
        upload(name, content)
    mk.upload_config = fail_at_the_record
    with pytest.raises(MoonrakerError):
        run_save_currents(mk, [('2209', 'stepper_x', 0.8)])
    mk.upload_config, mk.scripts = upload, []
    backup(mk, monkeypatch)
    assert console(mk)[0].startswith('The last save left no record of the lines it changed')


@pytest.mark.parametrize('record', [True, False])
def test_the_console_hears_what_goes_back_before_an_upload_fails(monkeypatch, record):
    # the files may be half written by then: the console has named them already
    from chopper_autotune.moonraker import MoonrakerError
    mk = saved_registers() if record else FakeMoonraker({
        'printer.cfg': BEFORE_SAVE + AUTOSAVE, 'chopper-autotune/backups/printer.cfg.bak': BEFORE_SAVE})

    def fail(name, content):
        raise MoonrakerError('cannot upload %s' % name)
    mk.upload_config = fail
    with pytest.raises(MoonrakerError):
        backup(mk, monkeypatch)
    assert console(mk) and console(mk)[-1].endswith('Klipper restarts now')


def test_a_missing_record_reads_as_none_and_other_failures_raise(monkeypatch):
    import io
    import urllib.error

    from chopper_autotune.moonraker import Moonraker, MoonrakerError

    def answer(code):
        def urlopen(req, timeout):
            raise urllib.error.HTTPError(req.full_url, code, 'status %d' % code, {}, io.BytesIO())
        monkeypatch.setattr('urllib.request.urlopen', urlopen)
    answer(404)
    assert Moonraker().download_config(LAST_SAVE, missing_ok=True) is None
    with pytest.raises(MoonrakerError):
        Moonraker().download_config('printer.cfg')
    answer(500)
    with pytest.raises(MoonrakerError):
        Moonraker().download_config(LAST_SAVE, missing_ok=True)


@pytest.mark.parametrize('name, fields, filename', [
    (LAST_SAVE, {'root': 'config', 'path': 'chopper-autotune'}, 'last-save.json'),
    ('chopper-autotune/backups/config/steppers.cfg.bak',
     {'root': 'config', 'path': 'chopper-autotune/backups/config'}, 'steppers.cfg.bak'),
    ('printer.cfg', {'root': 'config'}, 'printer.cfg')])
def test_an_upload_names_its_folder_where_moonraker_creates_it(monkeypatch, name, fields, filename):
    # Moonraker creates the folders of the path field; a folder in the file name must exist
    import contextlib
    import io

    from chopper_autotune.moonraker import Moonraker
    sent = []

    def urlopen(req, timeout):
        sent.append(req.data.decode())
        return contextlib.closing(io.BytesIO(b'{}'))
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    Moonraker().upload_config(name, 'text')
    assert dict(re.findall(r'name="(\w+)"\r\n\r\n([^\r]*)\r\n', sent[0])) == fields
    assert re.findall(r'filename="([^"]*)"', sent[0]) == [filename]


def test_moonraker_lists_every_file_of_the_config_folder_and_deletes_one(monkeypatch):
    # the copies end in .bak: a listing of .cfg files alone would hide them from the restore
    import contextlib
    import io

    from chopper_autotune.moonraker import Moonraker
    mk = Moonraker('http://x')
    monkeypatch.setattr(mk, '_request', lambda method, path, params=None: [
        {'path': 'printer.cfg'}, {'path': 'chopper-autotune/backups/printer.cfg.bak'},
        {'path': 'chopper-autotune/last-save.json'}])
    assert mk.list_config_files() == ['printer.cfg', 'chopper-autotune/backups/printer.cfg.bak',
                                      'chopper-autotune/last-save.json']
    sent = []

    def urlopen(req, timeout):
        sent.append((req.get_method(), req.full_url))
        return contextlib.closing(io.BytesIO(b'{}'))
    monkeypatch.setattr('urllib.request.urlopen', urlopen)
    mk.delete_config('config/my steppers.chopper-backup.cfg')
    assert sent == [('DELETE', 'http://x/server/files/config/config/my%20steppers.chopper-backup.cfg')]


# a config folder: dated SAVE_CONFIG copies, nested and hidden folders, the tool's own files
TREE = ['printer.cfg', 'printer-20250922_211125.cfg', 'mainsail.cfg', 'moonraker.conf',
        'config/a.cfg', 'config/a.chopper-backup.cfg', 'config/b.cfg', 'config/.c.cfg',
        'config/sub/d.cfg', 'config/sub/deeper/e.cfg', '.old/f.cfg', 'config/.old/g.cfg',
        'chopper-autotune/last-save.json', 'chopper-autotune/backups/printer.cfg.bak',
        'chopper-autotune/backups/config/a.cfg.bak']


@pytest.mark.parametrize('pattern', [
    'mainsail.cfg', 'config/a.cfg', '*.cfg', 'config/*.cfg', 'config/*', '*/*.cfg', '*/*/*.cfg',
    '**/*.cfg', 'config/**/*.cfg', 'config/**', '**', '**/*', 'config/?.cfg', 'config/[ab].cfg',
    'config/.*', '.*/*.cfg', 'config/.old/*.cfg', 'config/**/.*', '**.cfg', 'chopper-autotune/**/*.cfg'])
def test_an_include_pattern_takes_what_glob_takes(tmp_path, pattern):
    # Klipper and Kalico resolve [include] with glob.glob: Kalico recursive, Klipper not,
    # where ** is a plain *
    import glob

    from chopper_autotune.analyze import glob_takes
    for name in TREE:
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text('')

    def taken(recursive):
        return {os.path.relpath(path, tmp_path)
                for path in glob.glob(os.path.join(str(tmp_path), pattern), recursive=recursive)
                if os.path.isfile(path)}
    ours = {name for name in TREE if glob_takes(pattern, name)}
    assert ours == taken(True)
    assert taken(False) <= ours


def test_an_include_counts_as_klipper_reads_it():
    # Klipper cuts each line at '#' first; an [include] must open the line
    from chopper_autotune.analyze import active_config_files
    files = {'printer.cfg': '[include config/*.cfg]  # [tmc2209 stepper_x] and [tmc2209 stepper_y]\n'
                            '# [include old.cfg]\n'
                            '[gcode_macro HOME]\ngcode:\n  [include indented.cfg]\n',
             'config/a.cfg': '[tmc2209 stepper_x]\n', 'old.cfg': '', 'indented.cfg': ''}
    assert list(active_config_files(FakeMoonraker(files))) == ['printer.cfg', 'config/a.cfg']
