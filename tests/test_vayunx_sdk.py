"""Python SDK on OpenTelemetry (spec F + spec A): automatic crypto hooks, OTLP export, safety, privacy, findings."""

import asyncio
import hashlib
import hmac
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

import pytest
import uvicorn

import vayunx
from profiler_service.api import create_app

SECRET = "correct horse battery staple #42"


@pytest.fixture(scope="module")
def live(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "sdk_test.db"
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
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


@pytest.fixture(autouse=True)
def _always_shut_down():
    yield
    vayunx.shutdown(timeout_s=1.0)  # restores every patched function between tests


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def start(live, **kw):
    rid = uuid.uuid4().hex
    st = vayunx.init(endpoint=live, service=kw.pop("service", "sdk-test"), variant=kw.pop("variant", "md5"), run_id=rid,
                     export_interval_s=0.2, **kw)
    return rid, st


def test_hooks_capture_fast_and_slow_crypto_without_code_changes(live):
    rid, st = start(live, slow_ms=0.5)
    assert st["initialized"] and st["hooks"]["hashlib.md5"] == "installed"
    for _ in range(1000):
        hashlib.md5(SECRET.encode()).digest()
    hmac.new(b"k" * 32, b"message", "sha256").digest()
    import argon2
    ph = argon2.PasswordHasher(time_cost=1, memory_cost=8 * 1024, parallelism=1)
    stored = ph.hash(SECRET)
    assert ph.verify(stored, SECRET)
    import bcrypt
    h = bcrypt.hashpw(SECRET.encode(), bcrypt.gensalt(rounds=4))
    assert bcrypt.checkpw(SECRET.encode(), h)
    hashlib.pbkdf2_hmac("sha256", SECRET.encode(), b"s" * 16, 1000)
    assert vayunx.shutdown(timeout_s=5.0) is True

    stats = get(f"{live}/v1/runs/{rid}/op-stats")
    by_alg = {}
    for s in stats:
        by_alg.setdefault((s["op_name"], s["attributes"].get("crypto.algorithm")), 0)
        by_alg[(s["op_name"], s["attributes"].get("crypto.algorithm"))] += s["count"]
    assert by_alg[("hash", "MD5")] == 1000
    assert by_alg[("mac", "HMAC-SHA256")] == 1
    assert by_alg[("hash", "Argon2id")] == 1 and by_alg[("verify", "Argon2id")] == 1
    assert by_alg[("hash", "bcrypt")] == 1 and by_alg[("verify", "bcrypt")] == 1
    assert by_alg[("kdf", "PBKDF2-HMAC-SHA256")] == 1

    spans = get(f"{live}/v2/runs/{rid}/spans")
    argon = [s for s in spans if s["attributes"].get("crypto.algorithm") == "Argon2id" and s["attributes"]["crypto.operation"] == "hash"]
    assert argon, "slow Argon2id call should also be a span"
    a = argon[0]["attributes"]
    assert a["crypto.params"] == "m=8192,t=1,p=1" and a["crypto.library"].startswith("argon2-cffi")
    assert a["crypto.input_bytes"] == len(SECRET.encode()) and a["crypto.sync"] is True and a["vayunx.cpu_time_ms"] >= 0
    run = get(f"{live}/v1/runs/{rid}")
    assert run["service"] == "sdk-test"
    wire = json.dumps(spans) + json.dumps(stats)
    assert SECRET not in wire and h.decode() not in wire and stored not in wire  # no secrets, no hashes


def test_host_exceptions_pass_through_unchanged(live):
    start(live)
    import bcrypt
    with pytest.raises(ValueError) as exc:
        bcrypt.checkpw(b"x", b"not-a-bcrypt-hash")
    assert type(exc.value) is ValueError
    with pytest.raises(TypeError):
        hashlib.md5(12345)  # the original TypeError, not a profiler error


def test_service_down_never_breaks_the_app_and_exit_is_bounded():
    vayunx.init(endpoint="http://127.0.0.1:9", service="offline-app", variant="x", export_interval_s=0.2)
    for _ in range(200):
        hashlib.sha256(b"data").digest()
    with vayunx.span("checkout"):
        hashlib.md5(b"x").digest()
    t = time.monotonic()
    vayunx.shutdown(timeout_s=2.0)
    assert time.monotonic() - t < 4.0


def test_blocking_hash_on_the_event_loop_is_a_finding(live):
    rid, _ = start(live, slow_ms=0.5)

    async def handler():
        hashlib.pbkdf2_hmac("sha256", b"pw", b"s" * 16, 20000)  # synchronous KDF on the loop thread

    asyncio.run(handler())
    hashlib.pbkdf2_hmac("sha256", b"pw", b"s" * 16, 20000)  # same call off the loop: no finding
    st = vayunx.status()
    assert st["findings"]["blocking_event_loop"] == 1
    vayunx.shutdown(timeout_s=5.0)
    spans = get(f"{live}/v2/runs/{rid}/spans")
    flagged = [s for s in spans if s["attributes"].get("vayunx.finding") == "blocking_event_loop"]
    assert len(flagged) == 1


def test_span_and_measure_api_nest_and_carry_cpu_time(live):
    rid, _ = start(live, slow_ms=0.0)

    @vayunx.measure("login")
    def login():
        with vayunx.span("load_user"):
            pass
        hashlib.pbkdf2_hmac("sha256", b"pw", b"s" * 16, 5000)

    login()
    vayunx.shutdown(timeout_s=5.0)
    spans = {s["span_name"]: s for s in get(f"{live}/v2/runs/{rid}/spans")}
    assert spans["load_user"]["parent_span_id"] == spans["login"]["span_id"]
    kdf = [s for n, s in spans.items() if s["attributes"].get("crypto.operation") == "kdf"][0]
    assert kdf["parent_span_id"] == spans["login"]["span_id"]
    assert spans["login"]["attributes"]["vayunx.cpu_time_ms"] > 0


def test_fast_hook_overhead_is_under_two_microseconds(live):
    start(live)
    n, pw = 20000, b"correct horse battery staple"
    orig = vayunx._core.original("hashlib.md5")
    best = []
    for _ in range(5):
        t = time.perf_counter_ns()
        for _ in range(n):
            orig(pw).digest()
        plain = time.perf_counter_ns() - t
        t = time.perf_counter_ns()
        for _ in range(n):
            hashlib.md5(pw).digest()
        hooked = time.perf_counter_ns() - t
        best.append((hooked - plain) / n)
    overhead = sorted(best)[len(best) // 2]
    print(f"hashlib.md5 hook overhead: {overhead:.0f} ns/call")
    assert overhead < 2000


def test_variant_from_environment_and_status(live, monkeypatch):
    monkeypatch.setenv("VAYUNX_VARIANT", "argon2id-env")
    monkeypatch.setenv("VAYUNX_SERVICE", "env-app")
    monkeypatch.setenv("VAYUNX_ENDPOINT", live)
    st = vayunx.init(export_interval_s=0.2)
    assert st["variant"] == "argon2id-env" and st["service"] == "env-app"
    hashlib.sha256(b"x").digest()
    vayunx.shutdown(timeout_s=5.0)
    run = get(f"{live}/v1/runs/{st['run_id']}")
    assert run["service"] == "env-app"


def test_launcher_hooks_before_app_imports(live, tmp_path):
    rid = uuid.uuid4().hex
    app = tmp_path / "app.py"
    app.write_text("from hashlib import md5\nfor _ in range(50):\n    md5(b'early-bound').digest()\n", encoding="utf-8")
    env = {**os.environ, "VAYUNX_ENDPOINT": live, "VAYUNX_SERVICE": "launched-app", "VAYUNX_VARIANT": "md5",
           "VAYUNX_RUN_ID": rid}
    proc = subprocess.run([sys.executable, "-m", "vayunx", "run", "--", sys.executable, str(app)], env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    stats = get(f"{live}/v1/runs/{rid}/op-stats")
    assert sum(s["count"] for s in stats if s["attributes"].get("crypto.algorithm") == "MD5") == 50


def _counts(stats):
    out = {}
    for s in stats:
        k = (s["op_name"], s["attributes"].get("crypto.algorithm"))
        out[k] = out.get(k, 0) + s["count"]
    return out


def test_library_hooks_record_the_outer_call_once(live):
    rid, st = start(live, slow_ms=1000.0)
    import jwt
    import nacl.pwhash
    import nacl.signing
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from passlib.context import CryptContext

    token = jwt.encode({"sub": "u1"}, "k" * 32, algorithm="HS256")
    assert jwt.decode(token, "k" * 32, algorithms=["HS256"])["sub"] == "u1"
    ctx = CryptContext(schemes=["pbkdf2_sha256"], pbkdf2_sha256__rounds=1000)
    assert ctx.verify(SECRET, ctx.hash(SECRET))
    stored = nacl.pwhash.argon2id.str(SECRET.encode(), opslimit=1, memlimit=8 * 1024 * 1024)
    assert nacl.pwhash.verify(stored, SECRET.encode())
    sk = nacl.signing.SigningKey.generate()
    sk.verify_key.verify(sk.sign(b"payload"))
    f = Fernet(Fernet.generate_key())
    assert f.decrypt(f.encrypt(b"payload")) == b"payload"
    PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=b"s" * 16, iterations=1000).derive(b"pw")
    aes = AESGCM(AESGCM.generate_key(bit_length=128))
    aes.decrypt(b"n" * 12, aes.encrypt(b"n" * 12, b"payload", None), None)
    # nested: hmac given the (hooked) hashlib.sha256 constructor records one MAC and no extra hash
    hmac.new(b"k" * 32, b"m", hashlib.sha256).digest()
    hooks = vayunx.status()["hooks"]
    assert vayunx.shutdown(timeout_s=5.0) is True

    c = _counts(get(f"{live}/v1/runs/{rid}/op-stats"))
    assert c[("sign", "HS256")] == 1 and c[("verify", "HS256")] == 1
    assert c[("hash", "passlib:pbkdf2_sha256")] == 1 and c[("verify", "passlib:pbkdf2_sha256")] == 1
    assert c[("hash", "Argon2id")] == 1 and c[("verify", "Argon2id")] == 1  # nacl
    assert c[("sign", "Ed25519")] == 1 and c[("verify", "Ed25519")] == 1
    assert c[("encrypt", "Fernet")] == 1 and c[("decrypt", "Fernet")] == 1
    assert c[("mac", "HMAC-SHA256")] == 1  # only the direct hmac.new call: none from JWT or Fernet
    assert ("hash", "SHA256") not in c
    assert c[("kdf", "PBKDF2-HMAC")] == 1  # cryptography's Rust PBKDF2HMAC hides hash and iterations
    assert ("kdf", "PBKDF2-HMAC-SHA256") not in c  # passlib's inner pbkdf2_hmac calls are not counted
    if hooks["cryptography.AESGCM.encrypt"] == "installed":
        assert c[("encrypt", "AES-GCM")] == 1 and c[("decrypt", "AES-GCM")] == 1


def test_shutdown_restores_every_patched_function(live):
    import bcrypt
    before = (hashlib.md5, hashlib.pbkdf2_hmac, hmac.new, bcrypt.hashpw)
    start(live)
    assert hashlib.md5 is not before[0]
    vayunx.shutdown(timeout_s=2.0)
    assert (hashlib.md5, hashlib.pbkdf2_hmac, hmac.new, bcrypt.hashpw) == before
    assert vayunx.status()["initialized"] is False


def test_offline_exporter_errors_are_rate_limited(caplog):
    vayunx.init(endpoint="http://127.0.0.1:9", service="offline-logs", variant="x", slow_ms=0.0)
    for _ in range(3):
        with vayunx.span("s"):
            pass
        vayunx._core._state.tp.force_flush(3000)
    vayunx.shutdown(timeout_s=2.0)
    otel = [r for r in caplog.records if r.name.startswith("opentelemetry.exporter")]
    assert len(otel) <= 1
