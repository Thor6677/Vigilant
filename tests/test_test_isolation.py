"""The suite must not start network-using background work or reach the network.

ISS-117/ISS-132: app startup under TestClient launched tasks that resolved and
connected to real hosts, and the event loop intermittently hung closing them.
"""
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import Settings, get_settings


def test_background_work_is_off_in_tests():
    # Fails if someone deletes the overrides from conftest.py.
    settings = get_settings()
    assert settings.background_jobs_enabled is False
    assert settings.sde_autoload_enabled is False


def _calls_made_by_startup(monkeypatch, enabled):
    calls = []

    async def _recorder():
        calls.append(1)

    monkeypatch.setattr(main, "ensure_sde_loaded", _recorder)
    monkeypatch.setattr(main, "_sde_autoload_enabled", lambda: enabled)
    with TestClient(main.app):
        pass
    return calls


def test_startup_skips_sde_load_when_disabled(monkeypatch):
    assert _calls_made_by_startup(monkeypatch, False) == []


def test_startup_loads_sde_when_enabled(monkeypatch):
    assert _calls_made_by_startup(monkeypatch, True) == [1]


def test_sde_autoload_defaults_to_enabled(monkeypatch):
    monkeypatch.delenv("SDE_AUTOLOAD_ENABLED", raising=False)
    s = Settings(eve_client_id="x", eve_client_secret="y", secret_key="z")
    assert s.sde_autoload_enabled is True


def _settings(**kw):
    base = dict(eve_client_id="x", eve_client_secret="y", secret_key="z")
    base.update(kw)
    return Settings(**base)


def test_sde_helper_reads_setting_true(monkeypatch):
    monkeypatch.setattr(main, "get_settings",
                        lambda: _settings(sde_autoload_enabled=True, background_jobs_enabled=False))
    assert main._sde_autoload_enabled() is True


def test_sde_helper_reads_setting_false(monkeypatch):
    monkeypatch.setattr(main, "get_settings",
                        lambda: _settings(sde_autoload_enabled=False, background_jobs_enabled=True))
    assert main._sde_autoload_enabled() is False
