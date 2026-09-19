"""Phase 4 exports: CSV (scorecard, per-trial, runs) and a PDF report - the same numbers as the screen."""

import csv
import io
import json
import time
import uuid

import pytest

from profiler_service.api import create_app
from profiler_service.auth import AuthConfig
from test_auth import Browser, serve
from test_projects import free_port


@pytest.fixture(scope="module")
def svc(tmp_path_factory, db_url):
    port = free_port()
    srv, t = serve(create_app(db_url(tmp_path_factory.mktemp("db")), auth=AuthConfig()), port)
    url, b = f"http://127.0.0.1:{port}", Browser()
    status, _, text = b.request(f"{url}/v2/lab/runs", "POST", {"presets": ["md5", "sha256"], "trials": 2, "duration_s": 0.3})
    assert status == 202
    exp_id = json.loads(text)["experiment_id"]
    for _ in range(300):
        if json.loads(b.request(f"{url}/v2/experiments/{exp_id}")[2])["status"] == "complete":
            break
        time.sleep(0.3)
    yield {"url": url, "exp": exp_id, "b": b}
    srv.should_exit = True
    t.join(timeout=10)


def get_raw(url):
    import urllib.request
    with urllib.request.urlopen(url, timeout=60) as r:
        return r.status, r.headers, r.read()


def test_scorecard_csv_has_the_same_numbers_as_the_screen(svc):
    url, exp = svc["url"], svc["exp"]
    status, headers, body = get_raw(f"{url}/v2/compare.csv?experiment_id={exp}")
    assert status == 200 and headers["Content-Type"].startswith("text/csv")
    assert "attachment" in headers["Content-Disposition"] and ".csv" in headers["Content-Disposition"]
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    screen = json.loads(svc["b"].request(f"{url}/v2/compare?experiment_id={exp}")[2])
    assert [r["variant"] for r in rows] == [v["key"] for v in screen["variants"]]
    for r, v in zip(rows, screen["variants"]):
        assert float(r["median_ns"]) == v["time_per_call"]["median_ns"] and float(r["p95_ns"]) == v["time_per_call"]["p95_ns"]
        assert r["safe_for_passwords"] == str(v["security"]["safe_for_passwords"]).lower()
        assert r["machine"] == screen["experiment"]["env"]["cpu_model"]
    assert rows[1]["change_vs_reference"] == screen["variants"][1]["vs_reference"]["time_change"]


def test_per_trial_csv(svc):
    _, _, body = get_raw(f"{svc['url']}/v2/compare.csv?experiment_id={svc['exp']}&kind=trials")
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    assert len(rows) == 4 and {r["variant"] for r in rows} == {"md5", "sha256"} and all(int(r["ops"]) > 0 for r in rows)


def test_runs_csv_defuses_spreadsheet_formulas(svc):
    url, b = svc["url"], svc["b"]
    b.request(f"{url}/v1/runs", "POST", {"run_id": uuid.uuid4().hex, "service": "=HYPERLINK(\"http://evil\")",
                                         "label": "+cmd", "phase": "baseline"})
    _, headers, body = get_raw(f"{url}/v2/runs.csv")
    rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
    evil = next(r for r in rows if "HYPERLINK" in r["service"])
    assert evil["service"].startswith("'=") and evil["label"] == "'+cmd"
    assert len(rows) == json.loads(b.request(f"{url}/v2/runs?limit=1")[2])["total"]


def test_pdf_report(svc):
    status, headers, body = get_raw(f"{svc['url']}/v2/compare.pdf?experiment_id={svc['exp']}")
    assert status == 200 and headers["Content-Type"] == "application/pdf"
    assert body.startswith(b"%PDF-") and body.rstrip().endswith(b"%%EOF") and len(body) > 2000
    for text in (b"MD5", b"SHA-256", b"weak data", b"Time per call"):
        assert text in body, text  # page streams are left uncompressed so the report's text is inspectable
    assert b"i5-" in body or b"Intel" in body or b"AMD" in body or b"Apple" in body  # the machine that produced it


def test_exports_follow_access_rules(svc):
    import urllib.error
    with pytest.raises(urllib.error.HTTPError) as e:
        get_raw(f"{svc['url']}/v2/compare.csv?experiment_id=does-not-exist")
    assert e.value.code == 404


def test_only_our_exact_change_format_escapes_the_formula_guard():
    from profiler_service.exports import safe_cell
    assert safe_cell("+15.8%") == "+15.8%" and safe_cell("-3.1% (not significant)") == "-3.1% (not significant)"
    for evil in ("-2+cmd|' /C calc'!A0", "+15.8%+cmd", "=1+1", "@SUM(A1)", "-1%\n=2"):
        assert safe_cell(evil).startswith("'"), evil
