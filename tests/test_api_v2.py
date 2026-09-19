"""/v2 API: presets, Lab jobs with progress, compare (lab and app), time series, search. v1 keeps working."""

import json
import socket
import threading
import time
import urllib.error
import urllib.request

import pytest
import uvicorn

from profiler_service.api import create_app
from profiler_service.db import make_engine, make_sessionmaker
from profiler_service.lab_jobs import LabJobs
from profiler_service.lab_store import LabStore
from profiler_service.models import Experiment
from vayunx_profiler_sdk import ProfilerClient


@pytest.fixture(scope="module")
def live(tmp_path_factory, db_url):
    app = create_app(db_url(tmp_path_factory.mktemp("db")))
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
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


def call(url, method="GET", body=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def wait_for(url, exp_id, statuses=("complete", "failed", "cancelled"), timeout=120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, exp = call(f"{url}/v2/experiments/{exp_id}")
        if exp["status"] in statuses:
            return exp
        time.sleep(0.3)
    raise AssertionError(f"experiment {exp_id} did not finish: {exp}")


def test_presets_have_security_context(live):
    status, presets = call(f"{live}/v2/presets")
    assert status == 200 and len(presets) == 8
    md5 = next(p for p in presets if p["id"] == "md5")
    assert md5["security"]["safe_for_passwords"] is False and md5["label"] == "MD5"
    assert md5["node"]["id"] == "md5@node" and md5["node"]["label"] == "MD5 · Node.js"
    assert isinstance(md5["node"]["available"], bool) and (md5["node"]["available"] or md5["node"]["reason"])


@pytest.mark.parametrize("body, text", [
    ({"presets": ["md5", "nope"]}, "unknown preset"),
    ({"presets": ["md5"]}, "at least two"),
    ({"presets": ["md5", "md5"]}, "at least two"),
    ({"presets": ["md5", "sha256"], "reference": "bcrypt-10"}, "reference"),
])
def test_lab_run_validation_is_friendly(live, body, text):
    status, out = call(f"{live}/v2/lab/runs", "POST", body)
    assert status == 422 and text in out["detail"]["error"] and out["detail"]["fix"]


def test_lab_experiment_end_to_end(live):
    status, out = call(f"{live}/v2/lab/runs", "POST", {"presets": ["md5", "sha256"], "trials": 2, "duration_s": 0.3})
    assert status == 202
    exp_id = out["experiment_id"]
    busy, detail = call(f"{live}/v2/lab/runs", "POST", {"presets": ["md5", "sha256"], "trials": 2, "duration_s": 0.3})
    assert busy == 409 and "already running" in detail["detail"]["error"]

    _, running = call(f"{live}/v2/experiments/{exp_id}")
    assert running["progress"]["total"] == 4 and "eta_s" in running["progress"]
    exp = wait_for(live, exp_id)
    assert exp["status"] == "complete" and exp["progress"]["percent"] == 100 and exp["env"]["cpu_model"]

    status, cmp = call(f"{live}/v2/compare?experiment_id={exp_id}")
    assert status == 200 and cmp["reference"] == "md5" and cmp["source"] == "lab"
    assert [v["key"] for v in cmp["variants"]] == ["md5", "sha256"] and cmp["verdict"]
    assert any(f["code"] == "few_trials" for f in cmp["variants"][1]["flags"])
    assert cmp["experiment"]["experiment_id"] == exp_id and cmp["capacity_defaults"]["cores_total"] >= 1

    _, other = call(f"{live}/v2/compare?experiment_id={exp_id}&reference=sha256&rate=500&cores=4")
    assert other["reference"] == "sha256" and other["variants"][1]["capacity"]["rate_per_s"] == 500
    assert other["capacity_defaults"]["cores_total"] == 4

    status, series = call(f"{live}/v2/experiments/{exp_id}/timeseries")
    assert status == 200 and len(series["regions"]) == 4
    assert {r["preset"] for r in series["regions"]} == {"md5", "sha256"}
    assert series["metrics"]["proc_cores_busy"]["points"] and series["metrics"]["machine_cpu_pct"]["unit"] == "%"

    _, exps = call(f"{live}/v2/experiments?source=lab&q=MD5")
    assert exps["total"] >= 1 and any(e["experiment_id"] == exp_id for e in exps["items"])

    _, runs = call(f"{live}/v2/runs?source=lab&q=SHA-256&limit=10")
    assert runs["total"] == 2 and all(r["variant"] == "SHA-256" for r in runs["items"])

    status, v1 = call(f"{live}/v1/runs?service=vayunx-lab")
    assert status == 200 and {r["phase"] for r in v1} == {"baseline", "remediated"}


def test_lab_experiment_can_be_cancelled(live):
    status, out = call(f"{live}/v2/lab/runs", "POST", {"presets": ["md5", "sha256"], "trials": 5, "duration_s": 1.0})
    assert status == 202
    exp_id = out["experiment_id"]
    time.sleep(0.5)
    assert call(f"{live}/v2/experiments/{exp_id}/cancel", "POST")[0] == 202
    assert wait_for(live, exp_id)["status"] == "cancelled"


def test_compare_app_runs_from_the_sdk(live):
    client = ProfilerClient(live, "demo-app")
    run_ids = []
    for variant, spin in (("md5", 200), ("argon2id", 20000)):
        with client.run(label=variant, phase="baseline" if variant == "md5" else "remediated") as run:
            op = run.op("hash_password", attributes={"crypto.operation": "hash", "crypto.algorithm": variant})
            for _ in range(200):
                with op:
                    sum(range(spin))
        run_ids.append(run.run_id)
    status, cmp = call(f"{live}/v2/compare?run_ids={','.join(run_ids)}")
    assert status == 200 and cmp["source"] == "app" and cmp["operation"] == "hash"
    assert [v["key"] for v in cmp["variants"]] == ["md5", "argon2id"]
    assert cmp["variants"][1]["vs_reference"]["time_ratio"] > 1 and "per call" in cmp["verdict"]


def test_app_compare_prefers_the_scoped_operation_over_unrelated_hashing(live):
    """Both runs also hash a large buffer outside any span (think ETags). The comparison must still be about
    the hashing done inside `login`, not whichever unscoped series happens to take more time."""
    import hashlib

    import argon2
    import vayunx

    big = b"x" * (32 * 1024 * 1024)
    run_ids = []
    for variant in ("md5", "argon2id"):
        st = vayunx.init(endpoint=live, service="scoped-app", variant=variant, export_interval_s=0.2, gauges=False)
        ph = argon2.PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1)
        for _ in range(3):
            with vayunx.span("login"):
                hashlib.md5(b"pw").digest() if variant == "md5" else ph.hash("pw")
            hashlib.sha256(big).digest()  # unrelated, unscoped, and slower than the login hashing
        assert vayunx.shutdown(timeout_s=5.0)
        run_ids.append(st["run_id"])
    status, cmp = call(f"{live}/v2/compare?run_ids={','.join(run_ids)}")
    assert status == 200, cmp
    assert cmp["operation"] == "login" and cmp["operation_kind"] == "span" and "hash" in cmp["other_operations"]
    assert cmp["operation_detail"] == {"md5": ["hash MD5"], "argon2id": ["hash Argon2id"]}
    assert [(v["security"]["algorithm"], v["security"]["safe_for_passwords"]) for v in cmp["variants"]] == \
        [("MD5", False), ("Argon2id", True)]
    md5, argon = cmp["variants"]
    # per-call CPU only where every call was CPU-timed (slow calls become spans); never process CPU / count
    assert md5["cpu"]["cpu_s_per_op"] is None and 0 < argon["cpu"]["cpu_s_per_op"] < 1
    assert argon["memory"]["mem_per_op_bytes"] is None and cmp["measurement_notes"]


def test_unknown_experiment_is_a_friendly_404(live):
    status, out = call(f"{live}/v2/experiments/does-not-exist")
    assert status == 404 and out["detail"]["error"] and out["detail"]["fix"]
    status, out = call(f"{live}/v2/compare")
    assert status == 422 and "experiment_id" in out["detail"]["fix"]


def test_stale_experiments_are_marked_failed_on_restart(tmp_path, db_url):
    Session = make_sessionmaker(make_engine(db_url(tmp_path)))
    exp_id = LabStore(Session).create_experiment(["md5", "sha256"], 5, 10.0, 1, "md5", "stale")
    LabStore(Session).set_status(exp_id, "running")
    LabJobs(Session).recover()
    with Session() as s:
        exp = s.get(Experiment, exp_id)
        assert exp.status == "failed" and "stopped" in exp.error
