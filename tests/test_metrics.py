import io

import numpy as np
import pytest

from chopper_autotune.metrics import parse_accel_csv, transients, trim, vibration_score

RATE = 3200.0
AMP = 1000.0


def make_csv(n=2000, freq=100.0):
    t = np.arange(n) / RATE
    ax = AMP * np.sin(2 * np.pi * freq * t)
    lines = ['#time,accel_x,accel_y,accel_z']
    lines += ['%.6f,%.3f,0.0,9800.0' % (ti, xi) for ti, xi in zip(t, ax)]
    return io.StringIO('\n'.join(lines))


def test_parse_rejects_malformed():
    with pytest.raises(ValueError):
        parse_accel_csv(io.StringIO('#h\n1,2,3\n'))


def test_trim_cuts_both_ends():
    data = np.arange(40).reshape(10, 4)
    assert len(trim(data, 0.25)) == 6
    assert trim(data, 0.0) is data


def test_vibration_score_on_synthetic_sine():
    score = vibration_score(parse_accel_csv(make_csv()), trim_fraction=0.25)
    assert score['samples'] == 1000
    assert score['sample_rate_hz'] == pytest.approx(RATE, rel=0.02)
    # median |A*sin| over uniform phase is A*sin(pi/4); gravity on Z must vanish via mean removal
    assert score['median_magnitude'] == pytest.approx(AMP * np.sin(np.pi / 4), rel=0.05)
    assert score['rms'] == pytest.approx(AMP / np.sqrt(2), rel=0.05)
    assert score['p95_magnitude'] < AMP * 1.01


def test_transients_counts_real_clicks_only():
    data = parse_accel_csv(make_csv())
    clean = transients(data)
    assert clean['clicks'] == 0
    assert clean['peak_ratio'] < 2

    # a real click: 40x the median for a few samples (hardware ones measure 22-69x);
    # a weak 5x bump (threshold-noise territory) must NOT count
    spiky = data.copy()
    spiky[300:305, 1] += 40 * AMP
    spiky[900:905, 1] += 5 * AMP
    result = transients(spiky)
    assert result['clicks'] == 1
    assert result['peak_ratio'] > 15
