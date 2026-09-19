"""Phase 4 share links: a read-only, expiring, revocable link to one comparison that works without signing in."""

import hashlib
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from fake_oidc import FakeIdP
from profiler_service.api import create_app
from profiler_service.auth import AuthConfig
from profiler_service.db import make_engine, make_sessionmaker
from profiler_service.models import ShareLink
from test_auth import Browser, serve
from test_projects import free_port


@pytest.fixture(scope="module")
def env(tmp_path_factory, db_url):
    with FakeIdP() as idp:
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        db = db_url(tmp_path_factory.mktemp("db"))
        cfg = AuthConfig(mode="oidc", issuer=idp.issuer, client_id=idp.client_id, public_url=url,
                         admin_emails=frozenset({"alice@example.com"}), session_secret="s")
        srv, t = serve(create_app(db, auth=cfg), port)

        def login(email):
            b = Browser()
            _, headers, _ = b.request(f"{url}/auth/login?next=/auth/me")
            b.follow(headers["Location"] + f"&login_hint={email}")
            return b

        alice, dave = login("alice@example.com"), login("dave@example.com")
        same = {"Origin": url}
        assert alice.request(f"{url}/v2/projects", "POST", {"name": "Secret"}, same)[0] == 201
        status, _, text = alice.request(f"{url}/v2/lab/runs", "POST", {"presets": ["md5", "sha256"], "trials": 1,
                                                                       "duration_s": 0.2, "project": "secret"}, same)
        assert status == 202, text
        exp = json.loads(text)["experiment_id"]
        for _ in range(300):
            if json.loads(alice.request(f"{url}/v2/experiments/{exp}")[2])["status"] == "complete":
                break
            time.sleep(0.3)
        yield {"url": url, "db": db, "alice": alice, "dave": dave, "same": same, "exp": exp}
        srv.should_exit = True
        t.join(timeout=10)


def share(env, who="alice", **body):
    body = {"experiment_id": env["exp"], "reference": "sha256", **body}
    status, _, text = env[who].request(f"{env['url']}/v2/shares", "POST", body, env["same"])
    return status, (json.loads(text) if text else None)


def test_anyone_with_the_link_sees_that_comparison_and_nothing_else(env):
    status, s = share(env)
    assert status == 201 and s["url"].startswith("/shared/vxs_") and s["expires_at"]
    token = s["url"].rsplit("/", 1)[1]
    anon = Browser()
    status, _, text = anon.request(f"{env['url']}/v2/shared/{token}")
    shared = json.loads(text)
    assert status == 200 and shared["reference"] == "sha256" and {v["key"] for v in shared["variants"]} == {"md5", "sha256"}
    assert shared["shared"]["expires_at"] == s["expires_at"] and "experiment_id" not in shared["shared"]
    assert anon.request(f"{env['url']}/v2/shared/{token}/timeseries")[0] == 200
    for path in (f"/v2/experiments/{env['exp']}", "/v2/runs", f"/v2/compare?experiment_id={env['exp']}"):
        assert anon.request(f"{env['url']}{path}")[0] == 401  # the link opens nothing else
    import urllib.request
    with urllib.request.urlopen(f"{env['url']}/v2/shared/{token}.pdf", timeout=60) as r:
        assert r.headers["Content-Type"] == "application/pdf" and r.read().startswith(b"%PDF-")
    with urllib.request.urlopen(f"{env['url']}/v2/shared/{token}.csv", timeout=60) as r:
        assert r.headers["Content-Type"].startswith("text/csv")
    Session = make_sessionmaker(make_engine(env["db"]))
    with Session() as db:
        link = db.get(ShareLink, s["share_id"])
        # one view: opening the comparison counts; its machine view and exports do not
        assert link.token_hash == hashlib.sha256(token.encode()).hexdigest() and link.views == 1


def test_you_can_only_share_what_you_can_see(env):
    status, _ = share(env, who="dave")
    assert status == 404
    assert share(env, expires_days=0)[0] == 422 and share(env, expires_days=91)[0] == 422


def test_revoked_and_expired_links_stop_working(env):
    _, s = share(env)
    token = s["url"].rsplit("/", 1)[1]
    assert env["alice"].request(f"{env['url']}/v2/shares/{s['share_id']}", "DELETE", headers=env["same"])[0] == 204
    assert Browser().request(f"{env['url']}/v2/shared/{token}")[0] == 410
    _, s2 = share(env)
    Session = make_sessionmaker(make_engine(env["db"]))
    with Session() as db:
        db.get(ShareLink, s2["share_id"]).expires_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=1)
        db.commit()
    assert Browser().request(f"{env['url']}/v2/shared/{s2['url'].rsplit('/', 1)[1]}")[0] == 410
    assert Browser().request(f"{env['url']}/v2/shared/vxs_not-a-real-token")[0] == 404


def test_people_see_and_revoke_their_own_links(env):
    _, s = share(env)
    mine = json.loads(env["alice"].request(f"{env['url']}/v2/shares")[2])
    assert any(x["share_id"] == s["share_id"] and "url" not in x for x in mine)  # the token is shown only once
    assert json.loads(env["dave"].request(f"{env['url']}/v2/shares")[2]) == []
    assert env["dave"].request(f"{env['url']}/v2/shares/{s['share_id']}", "DELETE", headers=env["same"])[0] == 404
