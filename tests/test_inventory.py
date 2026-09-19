"""Phase 4 crypto inventory: which algorithms each app was seen using, with parameters checked against OWASP."""

import csv
import hashlib
import io
import json
import urllib.request
import uuid

import pytest

import vayunx
from profiler_service.api import create_app
from profiler_service.auth import AuthConfig
from test_auth import Browser, serve
from test_projects import free_port
from vayunx_lab.security import owasp_check


@pytest.mark.parametrize("algorithm, params, meets", [
    ("Argon2id", "m=19456,t=2,p=1", True),
    ("Argon2id", "m=47104,t=1,p=1", True),   # an OWASP-equivalent configuration
    ("Argon2id", "m=65536,t=3,p=4", True),
    ("Argon2id", "m=8192,t=1,p=1", False),
    ("Argon2id", "m=12288,t=2,p=1", False),  # memory of the t=3 option, but only t=2
    ("bcrypt", "cost=10", True), ("bcrypt", "cost=4", False),
    ("PBKDF2-HMAC-SHA256", "i=600000", True), ("PBKDF2-HMAC-SHA256", "iterations=600000", True),
    ("PBKDF2-HMAC-SHA256", "i=1000", False),
    ("scrypt", "n=131072,r=8,p=1", True), ("scrypt", "N=131072,r=8,p=1", True), ("scrypt", "n=16384,r=8,p=1", False),
    ("Argon2id", None, None), ("PBKDF2-HMAC-SHA1", "i=600000", None), ("MD5", None, False),
])
def test_owasp_minimums(algorithm, params, meets):
    got, why = owasp_check(algorithm, params)
    assert got is meets and why


@pytest.fixture(scope="module")
def svc(tmp_path_factory, db_url):
    port = free_port()
    srv, t = serve(create_app(db_url(tmp_path_factory.mktemp("db")), auth=AuthConfig()), port)
    url = f"http://127.0.0.1:{port}"
    import argon2
    ph = argon2.PasswordHasher(time_cost=1, memory_cost=8192, parallelism=1)  # below the OWASP minimum
    for variant in ("v1", "v2"):
        vayunx.init(endpoint=url, service="inv-app", variant=variant, run_id=uuid.uuid4().hex, export_interval_s=0.2, gauges=False)
        with vayunx.span("login"):
            hashlib.md5(b"pw").digest()
            ph.hash("pw")
        hashlib.sha256(b"etag").digest()
        assert vayunx.shutdown(timeout_s=5.0)
    b = Browser()
    b.request(f"{url}/v2/lab/runs", "POST", {"presets": ["md5", "bcrypt-10"], "trials": 1, "duration_s": 0.2})
    yield {"url": url, "b": b}
    srv.should_exit = True
    t.join(timeout=10)


def test_inventory_lists_what_each_app_was_seen_using(svc):
    status, _, text = svc["b"].request(f"{svc['url']}/v2/inventory")
    inv = json.loads(text)
    assert status == 200 and [a["service"] for a in inv["apps"]] == ["inv-app"]  # Lab runs are not apps
    items = {(i["operation"], i["algorithm"]): i for i in inv["apps"][0]["algorithms"]}
    assert set(items) == {("hash", "MD5"), ("hash", "Argon2id"), ("hash", "SHA256")}
    argon = items[("hash", "Argon2id")]
    assert argon["params"] == "m=8192,t=1,p=1" and argon["safe_for_passwords"] is True
    assert argon["meets_owasp_minimum"] is False and "19456" in argon["owasp_note"]
    assert argon["scopes"] == ["login"] and argon["runs"] == 2 and argon["calls"] == 2 and argon["variants"] == ["v1", "v2"]
    assert argon["library"].startswith("argon2-cffi") and argon["runtime"].startswith("CPython")
    assert argon["key"] == "inv-app|hash|Argon2id|m=8192,t=1,p=1"
    assert items[("hash", "MD5")]["safe_for_passwords"] is False and items[("hash", "MD5")]["scopes"] == ["login"]
    assert items[("hash", "SHA256")]["scopes"] == []
    assert inv["apps"][0]["summary"] == {"algorithms": 3, "not_for_passwords": 2, "below_owasp_minimum": 1}


def test_inventory_csv(svc):
    with urllib.request.urlopen(f"{svc['url']}/v2/inventory.csv", timeout=60) as r:
        rows = list(csv.DictReader(io.StringIO(r.read().decode("utf-8-sig"))))
    assert len(rows) == 3 and {r["algorithm"] for r in rows} == {"MD5", "Argon2id", "SHA256"}
    assert next(r for r in rows if r["algorithm"] == "Argon2id")["meets_owasp_minimum"] == "false"
