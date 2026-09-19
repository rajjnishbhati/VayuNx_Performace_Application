"""Phase 4 CI gate: judge a run's measured latencies against rules like "login p95 < 250ms"."""

import hashlib
import json
import os
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET

import pytest

import vayunx
from profiler_service.api import create_app
from profiler_service.auth import AuthConfig
from profiler_service.gate import GateRuleError, parse_rule
from test_auth import Browser, serve
from test_projects import free_port


def test_rules_parse_with_units_and_reject_nonsense():
    r = parse_rule("login p95 < 250ms")
    assert (r.name, r.stat, r.op, r.limit) == ("login", "p95", "<", 250e6)
    assert parse_rule("hash p50 <= 2us").limit == 2000 and parse_rule("hash p50 <= 2µs").limit == 2000
    assert parse_rule("login calls >= 100").limit == 100 and parse_rule("verify max < 1.5s").limit == 1.5e9
    for bad in ("login p95 250ms", "login p42 < 1ms", "login p95 < fast", "login calls >= 1ms", "", "a b c d e f"):
        with pytest.raises(GateRuleError):
            parse_rule(bad)


@pytest.fixture(scope="module")
def svc(tmp_path_factory, db_url):
    port = free_port()
    srv, t = serve(create_app(db_url(tmp_path_factory.mktemp("db")), auth=AuthConfig()), port)
    url = f"http://127.0.0.1:{port}"
    ids = {}
    for variant, iterations in (("fast", 1000), ("slow", 60000)):
        rid = uuid.uuid4().hex
        vayunx.init(endpoint=url, service="gate-app", variant=variant, run_id=rid, export_interval_s=0.2, gauges=False)
        for _ in range(40):
            with vayunx.span("login"):
                hashlib.pbkdf2_hmac("sha256", b"pw", b"s" * 16, iterations)
        assert vayunx.shutdown(timeout_s=5.0)
        ids[variant] = rid
    yield {"url": url, "ids": ids}
    srv.should_exit = True
    t.join(timeout=10)


def gate(url, body):
    status, _, text = Browser().request(f"{url}/v2/gate", "POST", body)
    return status, json.loads(text)


def test_pass_fail_and_no_data(svc):
    url, ids = svc["url"], svc["ids"]
    status, out = gate(url, {"run_id": ids["fast"], "rules": ["login p95 < 250ms", "login calls >= 40"]})
    assert status == 200 and out["status"] == "pass" and all(c["status"] == "pass" for c in out["checks"])
    measured = out["checks"][0]["measured"]
    assert 0 < measured < 250e6 and out["checks"][0]["measured_text"].endswith(("µs", "ms"))
    status, out = gate(url, {"run_id": ids["slow"], "rules": ["login p95 < 0.5ms", "login calls >= 40"]})
    assert out["status"] == "fail" and [c["status"] for c in out["checks"]] == ["fail", "pass"]
    status, out = gate(url, {"run_id": ids["fast"], "rules": ["checkout p95 < 1s"]})
    assert out["status"] == "no_data" and "checkout" in out["checks"][0]["message"]
    status, out = gate(url, {"run_id": ids["fast"], "rules": ["login p95 < 1s"], "min_calls": 1000})
    assert out["status"] == "no_data" and "40 calls" in out["checks"][0]["message"]  # too few to judge is not a pass


def test_latest_run_of_a_service_and_variant(svc):
    status, out = gate(svc["url"], {"service": "gate-app", "variant": "slow", "rules": ["login p95 < 1s"]})
    assert status == 200 and out["run"]["run_id"] == svc["ids"]["slow"]
    status, out = gate(svc["url"], {"service": "no-such-app", "rules": ["login p95 < 1s"]})
    assert status == 404


def test_cli_exit_codes_junit_and_github_summary(svc, tmp_path):
    url, ids = svc["url"], svc["ids"]
    junit, summary = tmp_path / "gate.xml", tmp_path / "summary.md"
    env = {**os.environ, "GITHUB_STEP_SUMMARY": str(summary)}
    cli = [sys.executable, "-m", "vayunx", "gate", "--endpoint", url]
    ok = subprocess.run(cli + ["--run-id", ids["fast"], "--rule", "login p95 < 250ms", "--junit", str(junit)],
                        capture_output=True, text=True, env=env, timeout=60)
    assert ok.returncode == 0, ok.stderr + ok.stdout
    assert "PASS" in ok.stdout and "login p95" in ok.stdout
    suite = ET.parse(junit).getroot()
    assert suite.get("tests") == "1" and suite.get("failures") == "0"
    assert "login p95 < 250ms" in summary.read_text(encoding="utf-8")
    bad = subprocess.run(cli + ["--service", "gate-app", "--variant", "slow", "--rule", "login p95 < 0.5ms", "--junit", str(junit)],
                         capture_output=True, text=True, env=env, timeout=60)
    assert bad.returncode == 1 and "FAIL" in bad.stdout
    assert ET.parse(junit).getroot().get("failures") == "1"
    none = subprocess.run(cli + ["--run-id", ids["fast"], "--rule", "checkout p95 < 1s"], capture_output=True, text=True, timeout=60)
    assert none.returncode == 3 and "NO DATA" in none.stdout
    bad_rule = subprocess.run(cli + ["--run-id", ids["fast"], "--rule", "login p95 fast"], capture_output=True, text=True, timeout=60)
    assert bad_rule.returncode == 2
