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
    def __init__(self, files, printing=False):
        self.files = dict(files)
        self.printing = printing
        self.uploads = []
        self.scripts = []

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
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: object())
    saved = {}
    monkeypatch.setattr(analyze, 'run_save',
                        lambda mk, items, extruder_state=None: saved.update(
                            items=items, extruder=extruder_state))

    analyze.run_save_latest(argparse.Namespace(audible_weight=0.25, url='http://x'))

    assert set(called) == {'03_x', '02_y'}                 # newest tuning dataset per motor
    assert {m['axis'] for m, _ in saved['items']} == {'x', 'y'}
    assert saved['extruder'] is None


def test_run_save_latest_includes_the_extruder_winner(monkeypatch):
    import argparse

    from chopper_autotune import analyze
    state = {'driver': '2209', 'fields': {'tbl': 3, 'toff': 7, 'hstrt': 6, 'hend': 0}}
    monkeypatch.setattr(analyze, 'dataset_dirs', lambda: [])
    monkeypatch.setattr('chopper_autotune.extruder.load_winner_state', lambda: state)
    monkeypatch.setattr(analyze, 'Moonraker', lambda url: object())
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
