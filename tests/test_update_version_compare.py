"""Version comparison for the update checker.

Fails CLOSED throughout: anything unparseable, and anything compared against the
"dev" sentinel, returns False. That single rule is what keeps the dev instance
silent without needing a flag of its own — a source build reports version "dev"
and can therefore never decide that an update is available.
"""
from app.ops.version import is_newer, parse_version


def test_parses_with_and_without_v_prefix():
    assert parse_version("v1.2.3") == (1, 2, 3, "")
    assert parse_version("1.2.3") == (1, 2, 3, "")


def test_parses_prerelease_suffix():
    assert parse_version("v1.3.0-rc1") == (1, 3, 0, "rc1")


def test_unparseable_returns_none():
    assert parse_version("dev") is None
    assert parse_version("") is None
    assert parse_version(None) is None
    assert parse_version("garbage") is None


def test_patch_and_minor_ordering():
    assert is_newer("v1.2.3", "v1.2.2") is True
    assert is_newer("v1.2.2", "v1.2.3") is False
    assert is_newer("v2.0.0", "v1.99.99") is True


def test_numeric_not_lexicographic():
    assert is_newer("v1.10.0", "v1.9.0") is True
    assert is_newer("v1.9.0", "v1.10.0") is False


def test_equal_is_not_newer():
    assert is_newer("v1.2.3", "v1.2.3") is False


def test_dev_running_version_never_notifies():
    assert is_newer("v9.9.9", "dev") is False
    assert is_newer("v9.9.9", "") is False
    assert is_newer("v9.9.9", None) is False
    # A prerelease is older than its own release.
    assert is_newer("v1.3.0-rc1", "v1.3.0") is False
    assert is_newer("v1.3.0", "v1.3.0-rc1") is True
