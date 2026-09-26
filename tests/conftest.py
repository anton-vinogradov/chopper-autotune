import pytest

from chopper_autotune import collect


@pytest.fixture(autouse=True)
def current_klipper(request, monkeypatch):
    """The fake printers run a supported Klipper; tests marked version_gate check the
    gate itself (collect.require_current_klipper)."""
    if 'version_gate' not in request.keywords:
        monkeypatch.setattr(collect, 'require_current_klipper', lambda kl: None)


def pytest_configure(config):
    config.addinivalue_line('markers', 'version_gate: runs the real Klipper version check')
