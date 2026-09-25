"""The sidecar's handling of files in /control, a directory the app can write.

The sidecar holds far more privilege than the app, so nothing it reads or writes
there may be steered by what the app left behind: its writes never follow a
link, its reads only take regular files, and request fields reach status.json
only after they validate.
"""
import json
import os
import stat
import threading

import pytest

import updater.supervisor as sup

HOSTILE_TAG = "v1.2.3$(touch pwned)"
HOSTILE_ID = "abc`id`"


@pytest.fixture
def control(tmp_path, monkeypatch):
    ctl = tmp_path / "control"
    ctl.mkdir()
    monkeypatch.setattr(sup, "CONTROL", ctl)
    monkeypatch.setattr(sup, "ROOT", str(tmp_path))
    return ctl


@pytest.fixture
def victim(tmp_path):
    f = tmp_path / "victim.txt"
    f.write_text("ORIGINAL=1\n")
    return f


# ── Writes ──────────────────────────────────────────────────────────────────

def test_write_does_not_follow_a_link_at_the_old_temp_name(control, victim):
    (control / "status.json.tmp").symlink_to(victim)
    sup.write_json_atomic(control / "status.json", {"state": "running"})
    assert victim.read_text() == "ORIGINAL=1\n"
    assert json.loads((control / "status.json").read_text()) == {"state": "running"}


def test_write_replaces_a_link_at_the_target_rather_than_following_it(control, victim):
    target = control / "status.json"
    target.symlink_to(victim)
    sup.write_json_atomic(target, {"n": 1})
    assert victim.read_text() == "ORIGINAL=1\n"
    assert not target.is_symlink()
    assert json.loads(target.read_text()) == {"n": 1}


def test_write_uses_a_fresh_file_every_time(control, monkeypatch):
    """mkstemp's O_EXCL is what makes a pre-placed name harmless; make sure
    the temp path really comes from it rather than a fixed name."""
    seen = []
    real = sup.tempfile.mkstemp

    def spy(*a, **k):
        fd, name = real(*a, **k)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(sup.tempfile, "mkstemp", spy)
    sup.write_json_atomic(control / "status.json", {"n": 1})
    sup.write_json_atomic(control / "status.json", {"n": 2})
    assert len(seen) == 2 and seen[0] != seen[1]
    assert all(os.path.dirname(n) == str(control) for n in seen)


def test_published_file_stays_readable_by_the_app(control):
    """The app reads these files as a different uid."""
    target = control / "status.json"
    sup.write_json_atomic(target, {"n": 1})
    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_a_failed_write_leaves_no_temp_file(control):
    with pytest.raises(TypeError):
        sup.write_json_atomic(control / "status.json", {"bad": object()})
    assert list(control.iterdir()) == []


# ── Reads ───────────────────────────────────────────────────────────────────

def test_a_linked_request_is_not_read_through(control, tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"id": "abc", "action": "update", "tag": "v1.2.3"}))
    (control / "request.json").symlink_to(outside)
    claimed, request = sup.claim_request(control)
    assert claimed is True
    assert request is None


def test_a_fifo_is_not_read(control):
    """A plain open() of a FIFO blocks until a writer appears, which would stall
    the poll loop. Run it on a thread so a regression fails instead of hanging."""
    os.mkfifo(control / "status.json")
    result = []
    t = threading.Thread(target=lambda: result.append(sup.read_json(control / "status.json")),
                         daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "read_json blocked on a FIFO"
    assert result == [None]


# ── Request fields in status.json ───────────────────────────────────────────

def _status(control) -> dict:
    return json.loads((control / "status.json").read_text())


def test_an_invalid_tag_is_refused_without_being_published(control, monkeypatch):
    published = []
    real = sup._publish
    monkeypatch.setattr(sup, "_publish", lambda s: (published.append(dict(s)), real(s)))

    status = sup.run_action({"id": "abc", "action": "update", "tag": HOSTILE_TAG})

    assert status["state"] == "failed"
    assert len(published) == 1, "nothing may be published before validation"
    raw = (control / "status.json").read_text()
    assert "touch" not in raw
    assert _status(control)["to_tag"] is None
    assert _status(control)["id"] == "abc"


def test_an_unknown_action_is_refused_without_being_published(control):
    sup.run_action({"id": "abc", "action": "$(reboot)", "tag": "v1.2.3"})
    raw = (control / "status.json").read_text()
    assert "reboot" not in raw
    assert _status(control)["action"] is None
    assert _status(control)["state"] == "failed"


def test_a_refusal_publishes_only_fields_that_validate(control):
    sup._publish_refusal({"id": HOSTILE_ID, "action": "update\n", "tag": HOSTILE_TAG}, "nope")
    status = _status(control)
    assert status["id"] is None
    assert status["action"] is None
    assert status["to_tag"] is None
    assert status["error"] == "nope"


def test_a_refusal_keeps_valid_fields(control):
    sup._publish_refusal({"id": "3f2b-11", "action": "rollback", "tag": "v1.2.3"}, "nope")
    status = _status(control)
    assert (status["id"], status["action"], status["to_tag"]) == ("3f2b-11", "rollback", "v1.2.3")


def test_a_request_with_an_unusable_id_is_refused(control):
    lock = control / "update.lock"
    (control / "request.json").write_text(
        json.dumps({"id": HOSTILE_ID, "action": "update", "tag": "v1.2.3"}))
    sup._tick([], lock)
    status = _status(control)
    assert status["state"] == "failed"
    assert "no id" in status["error"]
    assert status["id"] is None
    assert not lock.exists()
