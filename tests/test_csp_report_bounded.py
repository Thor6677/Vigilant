"""/csp-report caps the body it reads and the reports it processes (ISS-170).

The sink takes anonymous POSTs. It read the whole body, parsed it, and for a
JSON list walked every entry on the event loop: a row, a dedupe key and a
window per report, before the per-minute write cap dropped all but a few.
That cap and the window cap bound the disk and the sink's memory, not the
work one request could ask for. The page's report-uri sends one violation
per request, so a legitimate body is a couple of KB.
"""
import json

from starlette.testclient import TestClient

import app.main as main
import app.routes.csp as csp
from tests.test_csp_report_sink import _lines, _report, _sink


def _batch(n):
    """A Reporting API batch of n distinct violations."""
    return [{"type": "csp-violation", "body": {
        "documentURL": "https://example.test/page", "effectiveDirective": "script-src-elem",
        "blockedURL": "inline", "sourceFile": "https://example.test/a.js",
        "lineNumber": i, "columnNumber": 1, "disposition": "enforce"}} for i in range(n)]


def _oversized_body():
    body = json.dumps(_batch(1)) + " " * csp._MAX_BODY_BYTES
    assert len(body) > csp._MAX_BODY_BYTES
    return body.encode()


def test_a_body_over_the_cap_is_refused(tmp_path, monkeypatch):
    path, _clock = _sink(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        r = client.post("/csp-report", content=_oversized_body(),
                        headers={"content-type": "application/reports+json"})
    assert r.status_code == 413
    assert _lines(path) == [] and csp._windows == {}


def test_a_body_with_no_length_is_cut_off_at_the_cap(tmp_path, monkeypatch):
    """Sent chunked, so only counting what arrives can catch it."""
    path, _clock = _sink(tmp_path, monkeypatch)
    body = _oversized_body()
    chunks = (body[i:i + 4096] for i in range(0, len(body), 4096))
    with TestClient(main.app) as client:
        r = client.post("/csp-report", content=chunks,
                        headers={"content-type": "application/reports+json"})
    assert r.status_code == 413
    assert _lines(path) == [] and csp._windows == {}


def test_only_the_first_reports_of_a_batch_are_processed(tmp_path, monkeypatch):
    path, _clock = _sink(tmp_path, monkeypatch)
    batch = _batch(csp._MAX_REPORTS_PER_REQUEST * 5)
    assert len(json.dumps(batch)) < csp._MAX_BODY_BYTES
    with TestClient(main.app) as client:
        r = client.post("/csp-report", json=batch)
    assert r.status_code == 204
    assert len(csp._windows) == csp._MAX_REPORTS_PER_REQUEST
    assert sorted(row["line"] for row in _lines(path)) == list(range(csp._MAX_REPORTS_PER_REQUEST))


def test_a_legacy_report_still_lands(tmp_path, monkeypatch):
    path, _clock = _sink(tmp_path, monkeypatch)
    with TestClient(main.app) as client:
        assert client.post("/csp-report", json=_report()).status_code == 204
    (row,) = _lines(path)
    assert row["directive"] == "script-src-elem"
