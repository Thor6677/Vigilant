"""The /csp-report sink collapses repeats and refuses to fill the disk.

The sink is a diagnostic log, and a browser reports every violation as often
as it happens. A page that violates on a timer therefore reports on that
timer forever: one stale admin tab, refreshing itself every 10s with six
refused inline scripts per tick, wrote ~2,400 identical rows an hour and
rotated the 10 MB file out of existence daily — so the rows an investigation
wanted were always the ones that had just been rotated away.

These tests pin the two bounds that fix that: identical violations inside
the dedupe window produce ONE line carrying a count, and no minute can ever
write more than `_MAX_WRITES_PER_MIN` lines whatever arrives.
"""

import json

from starlette.testclient import TestClient

import app.main as main
import app.routes.csp as csp


def _report(doc="https://example.test/admin", line=1, col=23203,
            blocked="inline"):
    """A violation payload in the legacy report-uri shape browsers send."""
    return {"csp-report": {
        "document-uri": doc,
        "violated-directive": "script-src-elem",
        "blocked-uri": blocked,
        "source-file": "https://example.test/htmx.js",
        "line-number": line,
        "column-number": col,
        "disposition": "enforce",
    }}


class _Clock:
    """Hand-driven monotonic clock. The windows are minutes long; a test that
    waited them out in real time would take ten minutes to say anything."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


def _sink(tmp_path, monkeypatch):
    """Point the sink at a temp file and reset its in-process state.

    The state is module-level (one set of counters per worker), so a test
    that left it dirty would change the next test's answer.
    """
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(csp, "_LOG_DIR", log_dir)
    monkeypatch.setattr(csp, "_LOG_PATH", log_dir / "csp-violations.jsonl")
    monkeypatch.setattr(csp, "_windows", {})
    monkeypatch.setattr(csp, "_rate", [0.0, 0, 0])
    clock = _Clock()
    monkeypatch.setattr(csp, "_now", clock)
    return log_dir / "csp-violations.jsonl", clock


def _lines(path):
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln]


def test_identical_reports_collapse_to_one_line(tmp_path, monkeypatch):
    """The flood case. 50 reports of one violation, one line on disk."""
    path, _clock = _sink(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        for _ in range(50):
            assert client.post("/csp-report", json=_report()).status_code == 204

    rows = _lines(path)
    assert len(rows) == 1, rows
    assert rows[0]["count"] == 1
    assert rows[0]["directive"] == "script-src-elem"
    assert rows[0]["blocked"] == "inline"


def test_window_close_writes_the_suppressed_count(tmp_path, monkeypatch):
    """Nothing is lost, only merged: when the window closes, the repeats it
    swallowed are stated as a count on a single rollup line."""
    path, clock = _sink(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        for _ in range(12):
            client.post("/csp-report", json=_report())
        clock.advance(csp._DEDUPE_WINDOW_S + 1)
        # Any later report sweeps the expired windows — there is no timer.
        client.post("/csp-report", json=_report())

    rows = _lines(path)
    assert len(rows) == 3, rows  # first sighting, rollup, new window's first
    rollup = rows[1]
    assert rollup["count"] == 12
    assert rollup["window_s"] == csp._DEDUPE_WINDOW_S
    assert rollup["doc"] == rows[0]["doc"]
    assert rows[2]["count"] == 1


def test_distinct_violations_are_not_collapsed(tmp_path, monkeypatch):
    """Dedupe keys on the violation's identity, not on the endpoint. Two
    different blocked scripts must still be two rows, or the sink stops
    being able to answer 'which ones are left'."""
    path, _clock = _sink(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        client.post("/csp-report", json=_report(line=1))
        client.post("/csp-report", json=_report(line=2))
        client.post("/csp-report", json=_report(line=2))
        client.post("/csp-report", json=_report(doc="https://example.test/x"))

    rows = _lines(path)
    assert len(rows) == 3, rows
    assert [r["line"] for r in rows[:2]] == [1, 2]


def test_per_minute_cap_bounds_the_writes(tmp_path, monkeypatch):
    """The backstop for a client the dedupe cannot help: every report unique,
    so every one would otherwise be a line. The cap holds regardless."""
    path, clock = _sink(tmp_path, monkeypatch)
    over = csp._MAX_WRITES_PER_MIN * 3
    with TestClient(main.app) as client:
        for i in range(over):
            client.post("/csp-report", json=_report(line=i))

        assert len(_lines(path)) == csp._MAX_WRITES_PER_MIN

        # Next minute: the budget refills, and the drop is stated rather than
        # left to look like the violations simply stopped.
        clock.advance(61)
        client.post("/csp-report", json=_report(line=9999))

    rows = _lines(path)
    notice = rows[csp._MAX_WRITES_PER_MIN]
    assert notice["dropped"] == over - csp._MAX_WRITES_PER_MIN
    assert "cap" in notice["note"]
    assert rows[-1]["line"] == 9999


def test_one_json_object_per_line_is_preserved(tmp_path, monkeypatch):
    """The file format is what every reader of this log assumes. Dedupe adds
    fields to a row; it must never turn a row into something else."""
    path, clock = _sink(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        client.post("/csp-report", json=_report())
        client.post("/csp-report", json=_report())
        clock.advance(csp._DEDUPE_WINDOW_S + 1)
        client.post("/csp-report", json=_report(line=7))

    raw = path.read_text().splitlines()
    assert raw and all(json.loads(ln) for ln in raw)
    for row in _lines(path):
        assert isinstance(row, dict)
        assert "ts" in row


def test_modern_reporting_api_shape_still_logs(tmp_path, monkeypatch):
    """The sink accepts both wire formats; dedupe must not have quietly
    narrowed it to the legacy one."""
    path, _clock = _sink(tmp_path, monkeypatch)
    payload = [{"type": "csp-violation", "body": {
        "documentURL": "https://example.test/admin",
        "effectiveDirective": "script-src-elem",
        "blockedURL": "inline",
        "sourceFile": "https://example.test/htmx.js",
        "lineNumber": 1,
        "columnNumber": 23203,
        "disposition": "enforce",
    }}]
    with TestClient(main.app) as client:
        client.post("/csp-report", json=payload)

    rows = _lines(path)
    assert len(rows) == 1
    assert rows[0]["directive"] == "script-src-elem"
