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
    def __init__(self, files, printing=False, settings=None):
        self.files = dict(files)
        self.printing = printing
        self.config_settings = settings or {}
        self.uploads = []
        self.scripts = []

    def settings(self):
        return self.config_settings

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


def test_run_save_latest_saves_newest_tuning_dataset_per_motor(monkeypatch, tmp_path):
    import argparse

    from chopper_autotune import analyze
    from chopper_autotune.dataset import Dataset

    for name, manifest in [
        ('01_x', {'axis': 'x', 'search': 'grid'}),
        ('02_y', {'axis': 'y', 'search': 'descent'}),
        ('03_x', {'axis': 'x', 'search': 'descent'}),               # newer x -> this one wins
        ('04_x', {'axis': 'x', 'mode': 'find-speed'}),              # no 'search' -> ignored
        ('05_x', {'axis': 'x', 'mode': 'demo'}),                    # ignored
    ]:
        Dataset.create(tmp_path / name, manifest)

    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: sorted(tmp_path.iterdir()))
    called = []

    def fake_winner(root, weight):
        called.append(root.rsplit('/', 1)[-1])
        return (Dataset(root).manifest(), tmc.Chopper(0, 8, 7, 5))

    monkeypatch.setattr('chopper_autotune.tune.winner_of', fake_winner)
    monkeypatch.setattr('chopper_autotune.extruder.load_winner_state', lambda: None)
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: FakeMoonraker({}))
    saved = {}
    monkeypatch.setattr(analyze, 'run_save',
                        lambda mk, items, extruder_state=None: saved.update(
                            items=items, extruder=extruder_state))

    analyze.run_save_latest(argparse.Namespace(audible_weight=0.25, url='http://x'))

    assert set(called) == {'03_x', '02_y'}                 # newest tuning dataset per motor
    assert {m['axis'] for m, _ in saved['items']} == {'x', 'y'}
    assert saved['extruder'] is None


def test_run_save_latest_skips_a_motor_with_a_twin_and_saves_the_rest(monkeypatch, tmp_path, capsys):
    # AWD on X only: motor A would reach one driver of its pair; B and the extruder still save
    import argparse

    from chopper_autotune import analyze
    from chopper_autotune.dataset import Dataset
    for name, axis in (('01_x', 'x'), ('02_y', 'y')):
        Dataset.create(tmp_path / name, {'axis': axis, 'search': 'descent'})
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: sorted(tmp_path.iterdir()))
    monkeypatch.setattr('chopper_autotune.tune.winner_of',
                        lambda root, weight: (Dataset(root).manifest(), tmc.Chopper(0, 8, 7, 5)))
    state = {'driver': '2209', 'fields': {'tbl': 3, 'toff': 7, 'hstrt': 6, 'hend': 0}}
    monkeypatch.setattr('chopper_autotune.extruder.load_winner_state', lambda: state)
    monkeypatch.setattr(analyze, 'Moonraker',
                        lambda url: FakeMoonraker({}, settings={'stepper_x': {}, 'stepper_x1': {}}))
    saved = {}
    monkeypatch.setattr(analyze, 'run_save',
                        lambda mk, items, extruder_state=None: saved.update(
                            items=items, extruder=extruder_state))

    analyze.run_save_latest(argparse.Namespace(audible_weight=0.25, url='http://x'))

    assert [m['axis'] for m, _ in saved['items']] == ['y']
    assert saved['extruder'] == state
    assert 'motor A: NOT saving' in capsys.readouterr().out


def test_run_save_latest_includes_the_extruder_winner(monkeypatch):
    import argparse

    from chopper_autotune import analyze
    state = {'driver': '2209', 'fields': {'tbl': 3, 'toff': 7, 'hstrt': 6, 'hend': 0}}
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: [])
    monkeypatch.setattr('chopper_autotune.extruder.load_winner_state', lambda: state)
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: type('M', (), {'settings': lambda self: {}})())
    saved = {}
    monkeypatch.setattr(analyze, 'run_save',
                        lambda mk, items, extruder_state=None: saved.update(
                            items=items, extruder=extruder_state))

    # no axis datasets at all: the stored extruder winner alone is enough to save
    analyze.run_save_latest(argparse.Namespace(audible_weight=0.25, url='http://x'))
    assert saved['items'] == [] and saved['extruder'] == state


def test_run_save_latest_errors_without_datasets(monkeypatch):
    import argparse

    from chopper_autotune import analyze
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: [])
    monkeypatch.setattr('chopper_autotune.extruder.load_winner_state', lambda: None)
    with pytest.raises(SystemExit, match='no tuning datasets'):
        analyze.run_save_latest(argparse.Namespace(audible_weight=0.25, url='http://x'))


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
    # the extruder winner memory survives: SAVE_LAST must still be able to re-apply it
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
    assert [r['chopper'] for r in rank(rows, tmc.DRIVERS['2660'], 0.25)] == [tmc.Chopper(2, 4, 7, 8)]
    assert len(rank(rows, tmc.DRIVERS['2209'], 0.25)) == 2


def test_run_save_latest_skips_a_stale_extruder_winner(monkeypatch, capsys):
    import argparse

    from chopper_autotune import analyze
    stale = {'driver': '2660', 'fields': {'tbl': 2, 'toff': 4, 'hstrt': 7, 'hend': 10}}
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: [])
    monkeypatch.setattr('chopper_autotune.extruder.load_winner_state', lambda: stale)
    with pytest.raises(SystemExit, match='no tuning datasets'):
        analyze.run_save_latest(argparse.Namespace(audible_weight=0.25, url='http://x'))
    assert 'NOT saving the stored winner' in capsys.readouterr().out


def test_extruder_save_last_refuses_a_winner_klipper_would_not_load(monkeypatch, tmp_path):
    import json
    from types import SimpleNamespace

    from chopper_autotune import extruder
    state_file = tmp_path / 'extruder.json'
    state_file.write_text(json.dumps({'driver': '2660',
                                      'fields': {'tbl': 2, 'toff': 4, 'hstrt': 7, 'hend': 10}}))
    monkeypatch.setattr(extruder, 'STATE', str(state_file))
    mk = FakeMoonraker({'printer.cfg': CFG.replace('tmc2209 stepper_x', 'tmc2660 extruder')})
    monkeypatch.setattr('chopper_autotune.moonraker.Moonraker', lambda url: mk)
    with pytest.raises(SystemExit, match='Klipper refuses'):
        extruder.extruder_tune(None, SimpleNamespace(save_last=True, url='http://x'))
    assert mk.uploads == [] and mk.scripts == []


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


def test_run_save_latest_skips_a_managed_motor_and_names_why(monkeypatch):
    import argparse

    from chopper_autotune import analyze
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: ['/datasets/x'])
    monkeypatch.setattr(analyze, 'Dataset', lambda path: type('D', (), {'manifest': lambda self: {
        'axis': 'x', 'search': 'descent', 'driver': '2209'}})())
    monkeypatch.setattr('chopper_autotune.extruder.load_winner_state', lambda: None)
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: type('M', (), {'settings': lambda self: AUTOTUNE})())
    with pytest.raises(SystemExit, match=r'not saving \[tmc2209 stepper_x\]: autotune resets'):
        analyze.run_save_latest(argparse.Namespace(audible_weight=0.25, url='http://x'))


@pytest.mark.parametrize('command, driver, stepper', [('tune', '2240', 'stepper_x'),
                                                       ('extruder', '5160', 'extruder'),
                                                       ('save', '2209', 'stepper_y')])
def test_the_autotune_refusal_points_the_display_at_the_log(command, driver, stepper):
    # announce_failure shows '<command> FAILED: <message>' cut at 120 characters: a bare
    # "remove it" there would drop the StallGuard threshold sensorless homing stops on
    from chopper_autotune.collect import autotune_refusal
    shown = ('%s FAILED: %s' % (command, autotune_refusal(driver, stepper)))[:120]
    assert 'the log says what to do' in shown and 'remove' not in shown


@pytest.mark.parametrize('driver, section, lines', [
    ('2209', {'sg4_thrs': 80}, ['driver_SGTHRS: 80']),
    # SG4_THRS 0 too: it replaces an old line, and Klipper homes on SG4 when it is not 0
    ('2240', {'sgt': 2, 'sg4_thrs': 0}, ['driver_SGT: 2', 'driver_SG4_THRS: 0', 'driver_SLOPE_CONTROL: 3']),
    ('2240', {'sgt': 1, 'sg4_thrs': 60}, ['driver_SGT: 1', 'driver_SG4_THRS: 60', 'driver_SLOPE_CONTROL: 3']),
    ('5160', {'sgt': -4}, ['driver_SGT: -4']),
    ('2208', {}, []),
])
def test_the_advice_carries_over_what_autotune_sets(driver, section, lines):
    # Klipper records autotune's defaults in the settings too; its own README moves the
    # StallGuard thresholds from [tmc...] into [autotune_tmc], so they must come back
    from chopper_autotune.collect import autotune_advice, autotune_carry_over
    settings = {'autotune_tmc stepper_x': section}
    assert autotune_carry_over(settings, driver, 'stepper_x') == lines
    advice = autotune_advice(settings, driver, 'stepper_x')
    assert all(line in advice for line in lines)
    # a TMC2208 has no CoolStep: its result stays good once autotune is off
    assert ('save the result already measured' if driver == '2208' else 'tune again') in advice


def test_a_winner_measured_under_autotune_is_not_saved_once_it_is_gone():
    # its CoolStep lowers the current under load: the optimum was found at another current
    mk = FakeMoonraker({'printer.cfg': CFG}, settings={})
    with pytest.raises(SystemExit, match='measured under autotune; the log says what to do'):
        run_save(mk, [({'driver': '2209', 'stepper': 'stepper_x', 'autotune': 'performance'},
                       tmc.Chopper(0, 8, 7, 5))])
    assert mk.uploads == [] and mk.scripts == []


def test_run_save_latest_skips_a_dataset_measured_under_autotune(monkeypatch):
    import argparse

    from chopper_autotune import analyze
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: ['/datasets/x'])
    monkeypatch.setattr(analyze, 'Dataset', lambda path: type('D', (), {'manifest': lambda self: {
        'axis': 'x', 'search': 'descent', 'driver': '2209', 'autotune': 'auto'}})())
    monkeypatch.setattr('chopper_autotune.extruder.load_winner_state', lambda: {
        'driver': '2209', 'fields': {'tbl': 1, 'toff': 3, 'hstrt': 5, 'hend': 2}, 'autotune': 'silent'})
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: type('M', (), {'settings': lambda self: {}})())
    monkeypatch.setattr('chopper_autotune.tune.winner_of', lambda path, weight: (
        {'driver': '2209', 'stepper': 'stepper_x', 'autotune': 'auto'}, tmc.Chopper(0, 8, 7, 5)))
    with pytest.raises(SystemExit, match=r'not saving \[tmc2209 stepper_x\]: measured under autotune'):
        analyze.run_save_latest(argparse.Namespace(audible_weight=0.25, url='http://x'))


def save_latest_with(monkeypatch, manifests, winners, settings, extruder=None):
    """run_save_latest over fake datasets: manifests by path, winners by path (or a
    SystemExit for an aborted-at-start one); returns what reached run_save."""
    import argparse

    from chopper_autotune import analyze
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: list(manifests))
    monkeypatch.setattr(analyze, 'Dataset', lambda path: type('D', (), {
        'manifest': lambda self: manifests[path]})())
    monkeypatch.setattr('chopper_autotune.extruder.load_winner_state', lambda: extruder)
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: type('M', (), {'settings': lambda self: settings})())

    def winner_of(path, weight):
        if isinstance(winners[path], BaseException):
            raise winners[path]
        return winners[path]
    monkeypatch.setattr('chopper_autotune.tune.winner_of', winner_of)
    saved = {}
    monkeypatch.setattr(analyze, 'run_save', lambda mk, items, extruder_state=None: saved.update(
        items=items, extruder=extruder_state))
    analyze.run_save_latest(argparse.Namespace(audible_weight=0.25, url='http://x'))
    return saved


def test_save_skips_the_autotune_motor_and_saves_the_other(monkeypatch):
    x = {'axis': 'x', 'search': 'descent', 'driver': '2209', 'stepper': 'stepper_x'}
    y = {'axis': 'y', 'search': 'descent', 'driver': '2209', 'stepper': 'stepper_y'}
    combo = tmc.Chopper(0, 8, 7, 5)
    saved = save_latest_with(monkeypatch, {'/d/x': x, '/d/y': y},
                             {'/d/x': (x, combo), '/d/y': (y, combo)},
                             {'autotune_tmc stepper_x': {'tuning_goal': 'performance'}})
    assert saved['items'] == [(y, combo)]


def test_an_aborted_autotune_dataset_does_not_hide_an_older_result(monkeypatch):
    # the newest dataset, measured under autotune, stopped before any measurement:
    # the older complete one (no autotune then) is still the motor's result
    old = {'axis': 'x', 'search': 'descent', 'driver': '2209', 'stepper': 'stepper_x'}
    new = dict(old, autotune='auto')
    combo = tmc.Chopper(0, 8, 7, 5)
    saved = save_latest_with(monkeypatch, {'/d/old': old, '/d/new': new},
                             {'/d/old': (old, combo), '/d/new': SystemExit('no successful measurements')},
                             {})
    assert saved['items'] == [(old, combo)]
