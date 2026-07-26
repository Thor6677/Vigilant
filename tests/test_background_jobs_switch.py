"""Tests for BACKGROUND_JOBS_ENABLED.

The dev instance runs the same image as production but must not duplicate its
ingestion: two instances polling ESI/zKB under one CONTACT_EMAIL doubles the
traffic those operators attribute to a single contact, and both instances share
one spinning disk. One master switch is deliberately preferred over per-job
flags — the existing KILLMAILS_ENABLED / EVEREF_INGEST_ENABLED family already
provides per-job opt-in on top of it.

Rather than booting the whole app, these tests assert on the helper that
main.startup() consults, which is the unit that owns the decision.
"""
from app.config import Settings
from app.main import _background_jobs_enabled


def _settings(**kw):
    base = dict(eve_client_id="x", eve_client_secret="y", secret_key="z")
    base.update(kw)
    return Settings(**base)


def test_defaults_to_enabled():
    assert _settings().background_jobs_enabled is True


def test_helper_reads_setting_true(monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "get_settings",
                        lambda: _settings(background_jobs_enabled=True))
    assert _background_jobs_enabled() is True


def test_helper_reads_setting_false(monkeypatch):
    import app.main as main_mod
    monkeypatch.setattr(main_mod, "get_settings",
                        lambda: _settings(background_jobs_enabled=False))
    assert _background_jobs_enabled() is False
