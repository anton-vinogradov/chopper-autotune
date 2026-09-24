"""klipper_tmc_autotune writes its own chopper, StallGuard threshold, CoolStep and slope at
every Klipper start: what the tools read, put back and record on its motors."""
from types import SimpleNamespace

import pytest

from chopper_autotune import tmc
from chopper_autotune.collect import (check_resume, detect_hardware, live_chopper,
                                      resolve_autotune_baseline, resolve_stealth)

CHOPCONF = '// CHOPCONF:   14410153 toff=3 hstrt=5 hend=2 tbl=2 tpfd=4 mres=4(16usteps) intpol=1'


class Kl:
    def __init__(self, settings=None, chopconf=CHOPCONF, gconf='// GCONF: 00000000'):
        self.config = settings or {}
        self.answers = {'CHOPCONF': chopconf, 'GCONF': gconf}
        self.sent = []

    def settings(self):
        return self.config

    def object_list(self):
        return []

    def gcode_output(self, script):
        self.sent.append(script)
        return [self.answers[script.rsplit('=', 1)[1]]]


def hardware(autotune, baseline=None, stepper='stepper_x', driver='2240'):
    return SimpleNamespace(stepper=stepper, driver=tmc.DRIVERS[driver], autotune=autotune,
                           baseline=dict(baseline or {}), stealth=None)


def test_the_live_chopper_is_read_from_chopconf():
    assert live_chopper(Kl(), 'stepper_x', tmc.DRIVERS['2240']) == {
        'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 2, 'tpfd': 4}
    # Klipper prints only the non-zero fields: an absent one on a present line is 0
    kl = Kl(chopconf='// CHOPCONF:   00000003 toff=3')
    assert live_chopper(kl, 'stepper_x', tmc.DRIVERS['2209']) == {'tbl': 0, 'toff': 3, 'hstrt': 0, 'hend': 0}
    # a switched-off driver (toff 0) or no line at all is unreadable
    assert live_chopper(Kl(chopconf='// CHOPCONF: 00000000'), 'stepper_x', tmc.DRIVERS['2240']) is None
    assert live_chopper(Kl(chopconf='// ok'), 'stepper_x', tmc.DRIVERS['2240']) is None


def test_the_run_puts_back_autotunes_registers_read_live():
    # the config's driver_* lines are not what the driver ran: the re-home after the run
    # would go on a chopper autotune's StallGuard threshold was not tuned for
    hw = hardware('performance', baseline={'tbl': 1, 'toff': 8, 'hstrt': 7, 'hend': 5})
    resolve_autotune_baseline(Kl(), hw)
    assert hw.baseline == {'tbl': 2, 'toff': 3, 'hstrt': 5, 'hend': 2, 'tpfd': 4}
    # unreadable: the config stays, the run says a restart brings autotune's back
    hw = hardware('performance', baseline={'tbl': 1, 'toff': 8, 'hstrt': 7, 'hend': 5})
    resolve_autotune_baseline(Kl(chopconf='// ok'), hw)
    assert hw.baseline == {'tbl': 1, 'toff': 8, 'hstrt': 7, 'hend': 5}
    # no autotune: nothing is read, the config is the baseline
    kl, hw = Kl(), hardware(None, baseline={'tbl': 1})
    resolve_autotune_baseline(kl, hw)
    assert kl.sent == [] and hw.baseline == {'tbl': 1}


def test_a_live_spreadcycle_is_autotunes_doing_not_a_killed_run():
    # performance (and auto on X/Y) keeps spreadCycle whatever stealthchop_threshold says:
    # restoring stealthChop at the end would switch on a mode the driver never ran
    driver = tmc.DRIVERS['2209']
    for goal, expected in (('performance', None), ('auto', None), (None, driver.spreadcycle_switch)):
        settings = {'autotune_tmc stepper_x': {'tuning_goal': goal}} if goal else {}
        kl = Kl(settings, gconf='// GCONF: 00000004 en_spreadcycle=1')
        hw = hardware(goal, stepper='stepper_x', driver='2209')
        hw.stealth = driver.spreadcycle_switch                  # stealthchop_threshold > 0
        resolve_stealth(kl, hw)
        assert hw.stealth == expected, goal


def test_a_dataset_resumes_only_under_the_same_autotune_state():
    manifest = {'speeds': [58], 'accel': 1000.0, 'measure_time': 1.25, 'autotune': 'performance'}
    check_resume(manifest, [58], 1000.0, 1.25, 'performance')
    # the action first, within the display's 120 characters; 'off' instead of None
    with pytest.raises(SystemExit) as refused:
        check_resume(manifest, [58], 1000.0, 1.25, None)
    shown = ('collect FAILED: %s' % refused.value.code)[:120]
    assert shown.startswith('collect FAILED: refusing to resume: klipper_tmc_autotune was '
                            'performance, now off; start a new dataset')
    with pytest.raises(SystemExit, match='was off, now auto'):
        check_resume(dict(manifest, autotune=None), [58], 1000.0, 1.25, 'auto')
    # a manifest from before the key existed is not compared (collect stamps it then)
    del manifest['autotune']
    check_resume(manifest, [58], 1000.0, 1.25, 'auto')


def test_the_hardware_records_the_autotune_goal():
    settings = {'printer': {'kinematics': 'corexy', 'max_accel': 10000},
                'stepper_x': {'position_min': 0, 'position_max': 260},
                'stepper_y': {'position_min': 0, 'position_max': 260},
                'tmc2240 stepper_x': {}}
    assert detect_hardware(Kl(settings), 'x', accel=False).autotune is None
    settings['autotune_tmc stepper_x'] = {'tuning_goal': 'Silent'}
    assert detect_hardware(Kl(settings), 'x', accel=False).autotune == 'silent'
    settings['autotune_tmc stepper_x'] = {}
    assert detect_hardware(Kl(settings), 'x', accel=False).autotune == 'auto'


def test_the_collect_manifest_carries_the_autotune_goal():
    # the saved-result guard reads it from here
    import inspect

    from chopper_autotune import collect
    source = inspect.getsource(collect.collect)
    assert "'autotune': autotune_tag(hw.driver.name, hw.autotune)" in source
    # a resumed dataset from before the key gets it: the rest is measured now
    assert "ds.update_manifest(autotune=autotune_tag(hw.driver.name, hw.autotune))" in source


def test_a_tmc2208_records_no_autotune_measurement():
    # no CoolStep on a TMC2208: nothing lowered the current, the result stays savable
    from chopper_autotune.collect import autotune_tag
    assert autotune_tag('2208', 'auto') is None
    assert autotune_tag('2209', 'auto') == 'auto' and autotune_tag('2240', None) is None


def test_a_run_stopped_before_the_read_leaves_autotunes_motor_alone():
    # nothing was written yet: the config's registers and mode would replace autotune's
    from chopper_autotune.collect import exit_spreadcycle, restore_chopper
    scripts = []
    kl = SimpleNamespace(gcode=scripts.append)
    hw = hardware('performance', baseline={'tbl': 1, 'toff': 8, 'hstrt': 7, 'hend': 5})
    hw.settled, hw.stealth = False, tmc.DRIVERS['2240'].spreadcycle_switch
    restore_chopper(kl, hw)
    exit_spreadcycle(kl, hw)
    assert scripts == []
    # after the read the run puts back what it read
    hw.settled = True
    restore_chopper(kl, hw)
    assert scripts and 'VALUE=8' in scripts[0]
    # a motor without autotune keeps repairing what a killed run left behind
    scripts.clear()
    plain = hardware(None, baseline={'tbl': 1, 'toff': 8, 'hstrt': 7, 'hend': 5})
    plain.settled = False
    restore_chopper(kl, plain)
    assert scripts


@pytest.mark.parametrize('driver, autotune, header', [
    ('2209', None, 'Recommended for printer.cfg:'),
    ('2209', 'auto', 'Best measured (not for saving: klipper_tmc_autotune managed the motor'),
    # a TMC2208 carries no CoolStep tag, yet autotune still writes its own chopper at start
    ('2208', 'auto', 'Best measured (klipper_tmc_autotune ([autotune_tmc stepper_x]) writes'),
])
def test_the_recommendation_header_follows_autotune(tmp_path, monkeypatch, capsys, driver, autotune,
                                                     header):
    from chopper_autotune import dataset as dataset_mod
    from chopper_autotune.collect import report_winner
    from chopper_autotune.dataset import Dataset
    monkeypatch.setattr(dataset_mod, 'RESULTS_HOME', tmp_path)
    ds = Dataset.create(tmp_path / 'ds', {'mode': 'test'})
    for direction in (1, -1):
        ds.append({'id': 'a_%d' % direction, 'kind': 'move', 'status': 'ok',
                   **tmc.Chopper(0, 2, 4, 7).fields(), 'tpfd': None,
                   'score': {'median_magnitude': 1000.0, 'clicks': 0}})
    hw = SimpleNamespace(driver=tmc.DRIVERS[driver], stepper='stepper_x', autotune=autotune)
    report_winner(hw, ds, SimpleNamespace(trim=0.1, audible_weight=0.25),
                  SimpleNamespace(final=lambda text: None), top=5)
    assert header in capsys.readouterr().out
