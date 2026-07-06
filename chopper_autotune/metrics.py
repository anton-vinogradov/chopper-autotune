"""Vibration metrics computed from raw Klipper accelerometer CSV."""
from __future__ import annotations

import numpy as np


def parse_accel_csv(fileobj) -> np.ndarray:
    data = np.loadtxt(fileobj, delimiter=',', skiprows=1, ndmin=2)
    if data.shape[1] != 4 or data.shape[0] < 16:
        raise ValueError('unexpected accelerometer csv shape %s' % (data.shape,))
    return data


def trim(data: np.ndarray, fraction: float) -> np.ndarray:
    """Cut acceleration/deceleration transients at both ends of the move."""
    n = int(len(data) * fraction)
    return data[n:len(data) - n] if n else data


def window(data: np.ndarray, start: float, end: float) -> np.ndarray:
    """Slice rows whose time column falls into [start, end]."""
    return data[(data[:, 0] >= start) & (data[:, 0] <= end)]


def vibration_score(data: np.ndarray, trim_fraction: float = 0.25) -> dict:
    steady = trim(data, trim_fraction)
    t = steady[:, 0]
    accel = steady[:, 1:] - steady[:, 1:].mean(axis=0)
    magnitude = np.linalg.norm(accel, axis=1)
    duration = float(t[-1] - t[0])
    return {
        'samples': int(len(steady)),
        'sample_rate_hz': round(len(steady) / duration, 1) if duration > 0 else None,
        'median_magnitude': float(np.median(magnitude)),
        'p95_magnitude': float(np.percentile(magnitude, 95)),
        'rms': float(np.sqrt((accel ** 2).sum(axis=1).mean())),
    }


# Hardware-measured discrimination: real audible clicks peak at 22-69x the move's
# median, threshold-noise events stay below ~13x (see docs/SCIENCE.md, clicks case).
CLICK_RATIO = 15.0


def transients(data: np.ndarray) -> dict:
    """Click count over the WHOLE capture (ramps included): reversal clicks live outside
    the steady window that vibration_score deliberately slices to."""
    accel = data[:, 1:] - data[:, 1:].mean(axis=0)
    magnitude = np.linalg.norm(accel, axis=1)
    median = float(np.median(magnitude))
    if median <= 0:
        return {'clicks': 0, 'peak_ratio': None}
    above = magnitude > CLICK_RATIO * median
    return {
        'clicks': int(np.sum(above[1:] & ~above[:-1])),
        'peak_ratio': round(float(magnitude.max()) / median, 1),
    }
