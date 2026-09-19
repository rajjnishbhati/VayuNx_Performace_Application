"""Phase 4 sign-in: OIDC (authorization code + PKCE) for people, API tokens for SDKs and CI. Off by default."""

import hashlib
import json
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import parse_qs, urlparse

import pytest
import uvicorn
from sqlalchemy import select

from fake_oidc import FakeIdP
from profiler_service.api import create_app
from profiler_service.auth import AuthConfig
from profiler_service.db import make_engine, make_sessionmaker
from profiler_service.models import ApiToken, AuthSession


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Browser:
    """Follows nothing by itself; keeps cookies per host like a browser would (enough for these tests)."""

    def __init__(self):
        self.cookies: dict[str, dict[str, str]] = {}
        self.opener = urllib.request.build_opener(NoRedirect)

    def request(self, url, method="GET", body=None, headers=None):
        host = urlparse(url).netloc
        h = {"Content-Type": "application/json", **(headers or {})}
        jar = self.cookies.get(host, {})
        if jar:
            h["Cookie"] = "; ".join(f"{k}={v}" for k, v in jar.items())
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, method=method, headers=h)
        try:
            resp = self.opener.open(req, timeout=30)
        except urllib.error.HTTPError as e:
            resp = e
        for raw in resp.headers.get_all("Set-Cookie") or []:
            name, _, rest = raw.partition("=")
            value = rest.split(";", 1)[0]
            if "Max-Age=0" in raw or 'expires=Thu, 01 Jan 1970' in raw:
                self.cookies.setdefault(host, {}).pop(name, None)
            else:
                self.cookies.setdefault(host, {})[name] = value
        text = resp.read().decode()
        return resp.status if hasattr(resp, "status") else resp.code, resp.headers, text

    def follow(self, url, max_hops=6):
        """GET and follow redirects across hosts; returns the final (status, headers, body, url)."""
        for _ in range(max_hops):
            status, headers, body = self.request(url)
            if status not in (301, 302, 303, 307):
                return status, headers, body, url
            loc = headers["Location"]
            url = loc if loc.startswith("http") else f"{urlparse(url).scheme}://{urlparse(url).netloc}{loc}"
        raise AssertionError("too many redirects")


def serve(app, port):
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    return srv, t


@pytest.fixture(scope="module")
def env(tmp_path_factory, db_url):
    with FakeIdP() as idp:
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        url = f"http://127.0.0.1:{port}"
        db = db_url(tmp_path_factory.mktemp("db"))
        cfg = AuthConfig(mode="oidc", issuer=idp.issuer, client_id=idp.client_id, public_url=url,
                         admin_emails=frozenset({"alice@example.com"}), session_secret="test-secret")
        srv, t = serve(create_app(db, auth=cfg), port)
        yield {"idp": idp, "url": url, "db": db}
        srv.should_exit = True
        t.join(timeout=10)


def sign_in(env, who="alice@example.com", next_path="/v2/runs"):
    b = Browser()
    status, headers, _ = b.request(f"{env['url']}/auth/login?next={next_path}")
    assert status == 302
    authorize = headers["Location"] + f"&login_hint={who}"
    status, headers, body, final = b.follow(authorize)
    return b, status, body, final


def test_everything_needs_sign_in_when_auth_is_on(env):
    b = Browser()
    status, _, body = b.request(f"{env['url']}/v2/runs")
    detail = json.loads(body)["detail"]
    assert status == 401 and detail["login_url"] == "/auth/login" and detail["fix"]
    status, headers, _ = b.request(f"{env['url']}/")  # HTML page: sent to sign in, then back
    assert status == 302 and headers["Location"].startswith("/auth/login?next=")
    for path in ("/v1/runs", "/v1/traces", "/v2/presets"):
        assert b.request(f"{env['url']}{path}", "POST" if path == "/v1/traces" else "GET", {} if path == "/v1/traces" else None)[0] == 401


def test_oidc_sign_in_with_pkce_then_the_api_works(env):
    b, status, body, final = sign_in(env)
    assert status == 200 and final == f"{env['url']}/v2/runs" and "items" in json.loads(body)
    cookie = b.cookies[urlparse(env["url"]).netloc]["vayunx_session"]
    me = json.loads(b.request(f"{env['url']}/auth/me")[2])
    assert me["email"] == "alice@example.com" and me["is_admin"] is True and me["auth"] == "oidc"
    Session = make_sessionmaker(make_engine(env["db"]))
    with Session() as s:  # only a hash of the cookie is stored
        hashes = s.scalars(select(AuthSession.token_hash)).all()
        assert hashlib.sha256(cookie.encode()).hexdigest() in hashes and cookie not in hashes
    assert b.request(f"{env['url']}/auth/logout", "POST", headers={"Origin": env["url"]})[0] in (200, 303)
    assert b.request(f"{env['url']}/v2/runs")[0] == 401


@pytest.mark.parametrize("mode", ["bad_aud", "bad_iss", "expired", "bad_nonce", "bad_sig"])
def test_bad_id_tokens_are_refused(env, mode):
    env["idp"].mode = mode
    try:
        b, status, body, _ = sign_in(env, who="mallory@example.com")
    finally:
        env["idp"].mode = None
    assert status == 401 and "vayunx_session" not in b.cookies.get(urlparse(env["url"]).netloc, {})


def test_callback_rejects_a_forged_state(env):
    b = Browser()
    b.request(f"{env['url']}/auth/login?next=/")
    status, _, _ = b.request(f"{env['url']}/auth/callback?code=abc&state=forged")
    assert status == 400


def test_next_parameter_cannot_redirect_off_site(env):
    _, _, _, final = sign_in(env, next_path="https://evil.example/steal")
    assert urlparse(final).netloc == urlparse(env["url"]).netloc
    _, _, _, final = sign_in(env, next_path="//evil.example/x")
    assert urlparse(final).netloc == urlparse(env["url"]).netloc


def test_cookie_writes_need_a_same_origin_request(env):
    b, *_ = sign_in(env)
    body = {"presets": ["md5"]}  # invalid on purpose: we only care whether the request gets past the CSRF check
    assert b.request(f"{env['url']}/v2/lab/runs", "POST", body, {"Origin": "https://evil.example"})[0] == 403
    assert b.request(f"{env['url']}/v2/lab/runs", "POST", body)[0] == 403  # no Origin / Referer at all
    assert b.request(f"{env['url']}/v2/lab/runs", "POST", body, {"Origin": env["url"]})[0] == 422


def test_api_tokens_for_sdks_and_ci(env):
    b, *_ = sign_in(env)
    same = {"Origin": env["url"]}
    status, _, body = b.request(f"{env['url']}/v2/tokens", "POST", {"name": "ci"}, same)
    assert status == 201
    created = json.loads(body)
    token = created["token"]
    assert token.startswith("vx_") and len(token) > 40
    bearer = {"Authorization": f"Bearer {token}"}
    anon = Browser()
    run_id = uuid.uuid4().hex
    assert anon.request(f"{env['url']}/v1/runs", "POST", {"run_id": run_id, "service": "ci-app", "label": "x",
                                                          "phase": "baseline"}, bearer)[0] == 201
    assert anon.request(f"{env['url']}/v2/runs", headers=bearer)[0] == 200
    assert anon.request(f"{env['url']}/v2/tokens", "POST", {"name": "again"}, bearer)[0] == 403  # tokens can't mint tokens
    listed = json.loads(b.request(f"{env['url']}/v2/tokens")[2])
    assert any(t["token_id"] == created["token_id"] and "token" not in t for t in listed)
    Session = make_sessionmaker(make_engine(env["db"]))
    with Session() as s:
        stored = s.get(ApiToken, created["token_id"])
        assert stored.token_hash == hashlib.sha256(token.encode()).hexdigest() and token not in stored.prefix
    assert b.request(f"{env['url']}/v2/tokens/{created['token_id']}", "DELETE", headers=same)[0] == 204
    assert anon.request(f"{env['url']}/v2/runs", headers=bearer)[0] == 401
    assert anon.request(f"{env['url']}/v2/runs", headers={"Authorization": "Bearer vx_wrong"})[0] == 401


def test_sdks_send_their_token(env, monkeypatch):
    b, *_ = sign_in(env)
    token = json.loads(b.request(f"{env['url']}/v2/tokens", "POST", {"name": "sdk"}, {"Origin": env["url"]})[2])["token"]
    import hashlib as hl

    import vayunx
    monkeypatch.setenv("VAYUNX_API_TOKEN", token)
    st = vayunx.init(endpoint=env["url"], service="token-app", variant="md5", export_interval_s=0.2, gauges=True)
    for _ in range(10):
        hl.md5(b"x").digest()
    with vayunx.span("slow"):
        hl.pbkdf2_hmac("sha256", b"pw", b"s" * 16, 20000)
    assert vayunx.shutdown(timeout_s=5.0) is True
    bearer = {"Authorization": f"Bearer {token}"}
    stats = json.loads(Browser().request(f"{env['url']}/v1/runs/{st['run_id']}/op-stats", headers=bearer)[2])
    assert sum(s["count"] for s in stats if s["attributes"]["crypto.algorithm"] == "MD5") == 10
    spans = json.loads(Browser().request(f"{env['url']}/v2/runs/{st['run_id']}/spans", headers=bearer)[2])
    assert any(s["span_name"] == "slow" for s in spans)


def test_auth_off_is_the_default_and_needs_nothing(tmp_path, db_url):
    cfg = AuthConfig.from_env({})
    assert cfg.mode == "off"
    with pytest.raises(ValueError, match="VAYUNX_OIDC_ISSUER"):
        AuthConfig.from_env({"VAYUNX_AUTH": "oidc"})


def test_node_sdk_sends_its_token(env, tmp_path):
    import shutil
    import subprocess
    from pathlib import Path
    node = shutil.which("node")
    sdk = Path(__file__).resolve().parent.parent / "sdk-node" / "dist" / "register.js"
    if node is None or not sdk.exists():
        pytest.skip("Node.js or the built Node SDK (sdk-node/dist) is not available")
    b, *_ = sign_in(env)
    token = json.loads(b.request(f"{env['url']}/v2/tokens", "POST", {"name": "node"}, {"Origin": env["url"]})[2])["token"]
    app = tmp_path / "app.js"
    app.write_text("const c = require('crypto'); for (let i = 0; i < 25; i++) c.createHash('md5').update('x').digest();\n")
    run_id = uuid.uuid4().hex
    import os
    r = subprocess.run([node, "--require", str(sdk), str(app)], capture_output=True, text=True, timeout=60,
                       env={**os.environ, "VAYUNX_ENDPOINT": env["url"], "VAYUNX_SERVICE": "node-token-app",
                            "VAYUNX_RUN_ID": run_id, "VAYUNX_API_TOKEN": token})
    assert r.returncode == 0, r.stderr
    bearer = {"Authorization": f"Bearer {token}"}
    stats = json.loads(Browser().request(f"{env['url']}/v1/runs/{run_id}/op-stats", headers=bearer)[2])
    assert sum(s["count"] for s in stats if s["attributes"]["crypto.algorithm"] == "MD5") == 25
