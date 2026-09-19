"""Run list (no N+1, pagination) and friendly HTML errors. In-process server so SQL can be counted."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest
import uvicorn
from sqlalchemy import event

from profiler_service.api import create_app
from vayunx_profiler_sdk.transport import HttpTransport


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "runs_test.db"
    app = create_app(f"sqlite:///{db}")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}", app
    srv.should_exit = True
    thread.join(timeout=10)


def now():
    return datetime.now(timezone.utc).isoformat()


def make_run(t, service, phase="baseline", label="r"):
    rid = t.post("/v1/runs", {"service": service, "label": label, "phase": phase})["run_id"]
    t.post("/v1/spans", {"run_id": rid, "service": service, "category": "general", "span_id": "s1", "span_name": "x",
                         "start_time": now(), "end_time": now(), "duration_ms": 0.1})
    t.post("/v1/samples", {"run_id": rid, "service": service, "category": "general", "metric_name": "memory_mb",
                           "value": 10.0, "unit": "MiB", "timestamp": now()})
    return rid


def fetch(url):
    """(status, content-type, body) without raising on 4xx."""
    try:
        with urllib.request.urlopen(url) as r:
            return r.status, r.headers.get("content-type", ""), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("content-type", ""), e.read().decode()


def count_queries(app, fn):
    n = 0

    def on_execute(*_):
        nonlocal n
        n += 1

    event.listen(app.state.engine, "before_cursor_execute", on_execute)
    try:
        fn()
    finally:
        event.remove(app.state.engine, "before_cursor_execute", on_execute)
    return n


def test_run_list_query_count_does_not_grow_with_runs(live):
    url, app = live
    t = HttpTransport(url)
    for _ in range(2):
        make_run(t, "nplus1")
    small = count_queries(app, lambda: t.get("/v1/runs?service=nplus1"))
    for _ in range(10):
        make_run(t, "nplus1")
    large = count_queries(app, lambda: t.get("/v1/runs?service=nplus1"))
    assert len(t.get("/v1/runs?service=nplus1")) == 12
    assert large == small, f"queries grew with run count: {small} -> {large}"
    assert count_queries(app, lambda: fetch(f"{url}/")) <= small + 2  # index page: same grouped counts + total


def test_run_list_pagination(live):
    url, _ = live
    t = HttpTransport(url)
    ids = [make_run(t, "paging", label=f"r{i}") for i in range(5)]
    first = t.get("/v1/runs?service=paging&limit=2&offset=0")
    second = t.get("/v1/runs?service=paging&limit=2&offset=2")
    assert len(first) == 2 and len(second) == 2
    assert not {r["run_id"] for r in first} & {r["run_id"] for r in second}
    assert first[0]["run_id"] == ids[-1]  # newest first
    assert first[0]["span_count"] == 1 and first[0]["sample_count"] == 1
    status, _, _ = fetch(f"{url}/v1/runs?limit=0")
    assert status == 422


def test_index_page_paginates(live):
    url, _ = live
    t = HttpTransport(url)
    for i in range(3):
        make_run(t, "index-paging", label=f"i{i}")
    status, ctype, body = fetch(f"{url}/?page_size=2&page=2")
    assert status == 200 and "text/html" in ctype
    assert "Page 2" in body and "page=1" in body


def test_report_wrong_pair_is_a_friendly_html_page(live):
    url, _ = live
    t = HttpTransport(url)
    a, b = make_run(t, "html-errors"), make_run(t, "html-errors")
    status, ctype, body = fetch(f"{url}/report?baseline_run_id={a}&remediated_run_id={b}")
    assert status == 422 and "text/html" in ctype
    assert "expected &#x27;remediated&#x27;" in body or "expected 'remediated'" in body
    assert "Back to all runs" in body and 'href="/"' in body
    assert '{"detail"' not in body


def test_report_unknown_run_is_a_friendly_html_404(live):
    url, _ = live
    status, ctype, body = fetch(f"{url}/report?baseline_run_id=nope&remediated_run_id=nada")
    assert status == 404 and "text/html" in ctype and "Back to all runs" in body


def test_json_api_errors_are_unchanged(live):
    url, _ = live
    t = HttpTransport(url)
    a, b = make_run(t, "json-errors"), make_run(t, "json-errors")
    status, ctype, body = fetch(f"{url}/v1/comparison?baseline_run_id={a}&remediated_run_id={b}")
    assert status == 422 and "application/json" in ctype and "detail" in json.loads(body)
