"""Tests for the build version string and its use in the outbound User-Agent.

`VIGILANT_VERSION` is baked into the image by CI (see .github/workflows/
release.yml) and read by app.config.Settings. It is the single source of
truth for "which build is this", consumed by the outbound User-Agent and by
the update checker. `dev` is the deliberate default for source builds.

NOTE ON TEST STYLE — verified 2026-07-26. Because `version` uses
`validation_alias="VIGILANT_VERSION"` and Settings sets `extra="ignore"`,
passing `Settings(version="v1.2.3")` is SILENTLY IGNORED — the field still
resolves from the environment. A test written that way passes vacuously
without exercising anything. So: drive the field through the environment, and
use a plain stub for the user_agent tests rather than a real Settings.
"""
import app.config as config_mod
from app.config import Settings, user_agent


class _Stub:
    """Minimal stand-in for Settings — only what user_agent() reads."""

    def __init__(self, version, contact_email="a@example.com"):
        self.version = version
        self.contact_email = contact_email


def _settings():
    return Settings(eve_client_id="x", eve_client_secret="y", secret_key="z")


def test_version_defaults_to_dev(monkeypatch):
    monkeypatch.delenv("VIGILANT_VERSION", raising=False)
    assert _settings().version == "dev"


def test_version_reads_env(monkeypatch):
    monkeypatch.setenv("VIGILANT_VERSION", "v1.2.3")
    assert _settings().version == "v1.2.3"


def test_user_agent_strips_leading_v(monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", lambda: _Stub("v1.2.3"))
    assert user_agent() == "Vigilant/1.2.3 (a@example.com)"


def test_user_agent_dev_version(monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", lambda: _Stub("dev"))
    assert user_agent() == "Vigilant/dev (a@example.com)"


def test_user_agent_suffix_placement(monkeypatch):
    monkeypatch.setattr(config_mod, "get_settings", lambda: _Stub("v2.0.0"))
    assert user_agent("backfill") == "Vigilant/2.0.0 backfill (a@example.com)"
