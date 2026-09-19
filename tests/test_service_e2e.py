"""End-to-end: real uvicorn server (thread) + temp SQLite, driven through the SDK and raw HTTP."""

import socket
import threading
import time
import urllib.request
from datetime import datetime, timezone

import pytest
import uvicorn

from profiler_service.api import create_app
from vayunx_profiler_sdk import ProfilerClient, ProfilerHTTPError
from vayunx_profiler_sdk.transport import HttpTransport


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "profiler_test.db"
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(create_app(f"sqlite:///{db}"), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


def now():
    return datetime.now(timezone.utc).isoformat()


def status_of(fn):
    with pytest.raises(ProfilerHTTPError) as exc:
        fn()
    return exc.value.status


def test_sdk_nesting_sampling_and_report(server):
    client = ProfilerClient(server, "e2e-svc")
    run_ids = {}
    for phase, label, work in (("baseline", "fast", 0.001), ("remediated", "slow", 0.02)):
        with client.run(label=label, phase=phase) as run:
            client.start_sampling(interval_ms=20, metrics=["cpu_pct", "memory_mb"], category="cryptographic")
            for _ in range(2):
                with run.span("login", category="general") as outer:
                    with run.span("hash_password", category="cryptographic", attributes={"algorithm": label}) as inner:
                        with run.span("compute_digest", category="cryptographic"):
                            time.sleep(work)
                    assert inner.parent_span_id == outer.span_id
                assert outer.parent_span_id is None
            stats = client.stop_sampling()
            assert stats["errors"] == [] and stats["samples_sent"] > 0
        run_ids[phase] = run.run_id
        assert run.spans_sent == 6

    t = HttpTransport(server)
    assert t.get(f"/v1/runs/{run_ids['baseline']}")["completed_at"] is not None
    rep = t.get(f"/v1/comparison?baseline_run_id={run_ids['baseline']}&remediated_run_id={run_ids['remediated']}")
    fg = rep["baseline"]["flame_graph"]
    login = fg["children"][0]
    assert login["name"] == "login" and login["count"] == 2
    assert login["children"][0]["name"] == "hash_password" and login["children"][0]["children"][0]["name"] == "compute_digest"
    rows = {tuple(r["path"]): r for r in rep["span_comparison"]}
    digest = rows[("login", "hash_password", "compute_digest")]
    assert digest["status"] == "matched" and digest["delta_total_ms"] > 0
    assert {r["metric_name"] for r in rep["sampling_comparison"]} == {"cpu_pct", "memory_mb"}
    assert rep["summary"] and rep["baseline"]["categories"] == ["cryptographic", "general"]

    q = f"baseline_run_id={run_ids['baseline']}&remediated_run_id={run_ids['remediated']}"
    html = urllib.request.urlopen(f"{server}/report?{q}").read().decode()
    assert "BASELINE" in html and "REMEDIATED" in html and "d3-flame-graph@4.1.3" in html and "fast" in html and "slow" in html
    assert urllib.request.urlopen(f"{server}/").status == 200


def test_span_exception_is_recorded_and_run_still_completes(server):
    client = ProfilerClient(server, "e2e-errors")
    with pytest.raises(ZeroDivisionError):
        with client.run(label="boom", phase="baseline") as run:
            with run.span("divide"):
                1 / 0
    t = HttpTransport(server)
    assert t.get(f"/v1/runs/{run.run_id}")["completed_at"] is not None
    assert client.active_run is None


def test_ingestion_validation(server):
    t = HttpTransport(server)
    run = t.post("/v1/runs", {"service": "val-svc", "label": "v", "phase": "baseline"})
    rid = run["run_id"]

    def sp(**over):
        base = {"run_id": rid, "service": "val-svc", "category": "general", "span_id": "s1", "parent_span_id": None,
                "span_name": "x", "start_time": now(), "end_time": now(), "duration_ms": 0.5}
        base.update(over)
        return base

    assert t.post("/v1/spans", sp())["accepted"] == 1  # single object
    assert t.post("/v1/spans", [sp(span_id="s2", parent_span_id="s1"), sp(span_id="s3")])["accepted"] == 2  # batch
    assert status_of(lambda: t.post("/v1/spans", sp(span_id="s1"))) == 409  # duplicate across batches
    assert status_of(lambda: t.post("/v1/spans", [sp(span_id="d"), sp(span_id="d")])) == 409  # duplicate within batch
    assert status_of(lambda: t.post("/v1/spans", sp(span_id="m", service="other"))) == 422  # service mismatch
    assert status_of(lambda: t.post("/v1/spans", sp(span_id="c", category="crypto"))) == 422  # bad category
    assert status_of(lambda: t.post("/v1/spans", sp(span_id="n", start_time="2026-09-17T10:00:00"))) == 422  # naive timestamp
    assert status_of(lambda: t.post("/v1/spans", [])) == 422  # empty batch
    assert status_of(lambda: t.post("/v1/spans", sp(span_id="u", run_id="nope"))) == 404
    assert status_of(lambda: t.post("/v1/samples", {"run_id": rid, "service": "val-svc", "category": "general",
                                                      "metric_name": "cpu_pct", "value": float("nan"), "unit": "%", "timestamp": now()})) == 422
    t.post(f"/v1/runs/{rid}/complete", {})
    assert status_of(lambda: t.post("/v1/spans", sp(span_id="late"))) == 409  # completed run

    other = t.post("/v1/runs", {"service": "different-svc", "label": "o", "phase": "remediated"})["run_id"]
    same_phase = t.post("/v1/runs", {"service": "val-svc", "label": "b2", "phase": "baseline"})["run_id"]
    assert status_of(lambda: t.get(f"/v1/comparison?baseline_run_id={rid}&remediated_run_id={other}")) == 422
    assert status_of(lambda: t.get(f"/v1/comparison?baseline_run_id={rid}&remediated_run_id={same_phase}")) == 422


def test_sdk_guards(server):
    client = ProfilerClient(server, "guards")
    with pytest.raises(RuntimeError):
        client.start_sampling()
    with pytest.raises(ValueError):
        client.run(label="x", phase="pre")
    with client.run(label="x", phase="baseline") as run:
        with pytest.raises(ValueError):
            run.span("bad", category="crypto")
        with pytest.raises(ValueError):
            client.start_sampling(metrics=["gpu_pct"])
