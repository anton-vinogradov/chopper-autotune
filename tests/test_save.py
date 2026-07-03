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
    run_save(mk, {'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))

    assert mk.uploads == ['printer.chopper-backup.cfg', 'printer.cfg']
    assert mk.files['printer.chopper-backup.cfg'] == CFG
    assert 'driver_TOFF: 8' in mk.files['printer.cfg']
    assert mk.scripts == ['RESTART']
    assert 'Saved to printer.cfg' in capsys.readouterr().out


def test_run_save_refuses_when_printing():
    mk = FakeMoonraker({'printer.cfg': CFG}, printing=True)
    with pytest.raises(SystemExit, match='busy printing'):
        run_save(mk, {'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))
    assert mk.uploads == []


def test_run_save_refuses_ambiguous_sections():
    files = {'printer.cfg': CFG, 'extra.cfg': '[tmc2209 stepper_x]\nrun_current: 1\n'}
    mk = FakeMoonraker(files)
    with pytest.raises(SystemExit, match='several files'):
        run_save(mk, {'driver': '2209', 'stepper': 'stepper_x'}, tmc.Chopper(0, 8, 7, 5))
