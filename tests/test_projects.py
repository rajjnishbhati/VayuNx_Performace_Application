"""Phase 4 projects, teams and roles. Projects organise data even with sign-in off; with sign-in on they are
the access boundary: viewer < editor < admin, granted to teams; global administrators see everything."""

import json
import socket
import uuid

import pytest

from fake_oidc import FakeIdP
from profiler_service.api import create_app
from profiler_service.auth import AuthConfig
from test_auth import Browser, serve


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def new_run(b, url, project=None, headers=None, service="svc"):
    rid = uuid.uuid4().hex
    body = {"run_id": rid, "service": service, "label": "x", "phase": "baseline"}
    if project:
        body["project"] = project
    status, _, text = b.request(f"{url}/v1/runs", "POST", body, headers)
    return status, rid, text


# ----------------------------------------------------------------------------- sign-in off


@pytest.fixture(scope="module")
def open_service(tmp_path_factory, db_url):
    port = free_port()
    srv, t = serve(create_app(db_url(tmp_path_factory.mktemp("db")), auth=AuthConfig()), port)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(timeout=10)


def test_projects_organise_data_when_sign_in_is_off(open_service):
    url, b = open_service, Browser()
    projects = json.loads(b.request(f"{url}/v2/projects")[2])
    assert [p["project_id"] for p in projects] == ["default"] and projects[0]["my_role"] == "admin"
    status, _, text = b.request(f"{url}/v2/projects", "POST", {"name": "Payments API"})
    assert status == 201 and json.loads(text)["project_id"] == "payments-api"
    assert b.request(f"{url}/v2/projects", "POST", {"name": "Payments API"})[0] == 409
    assert new_run(b, url, "payments-api")[0] == 201
    assert new_run(b, url)[0] == 201  # no project given: Default
    assert new_run(b, url, "no-such-project")[0] == 404
    pay = json.loads(b.request(f"{url}/v2/runs?project=payments-api")[2])
    everything = json.loads(b.request(f"{url}/v2/runs")[2])
    assert pay["total"] == 1 and everything["total"] == 2 and pay["items"][0]["project_id"] == "payments-api"


# ----------------------------------------------------------------------------- sign-in on


@pytest.fixture(scope="module")
def org(tmp_path_factory, db_url):
    with FakeIdP() as idp:
        port = free_port()
        url = f"http://127.0.0.1:{port}"
        cfg = AuthConfig(mode="oidc", issuer=idp.issuer, client_id=idp.client_id, public_url=url,
                         admin_emails=frozenset({"alice@example.com"}), session_secret="s")
        srv, t = serve(create_app(db_url(tmp_path_factory.mktemp("db")), auth=cfg), port)

        def login(email):
            b = Browser()
            _, headers, _ = b.request(f"{url}/auth/login?next=/auth/me")
            b.follow(headers["Location"] + f"&login_hint={email}")
            return b

        people = {name: login(f"{name}@example.com") for name in ("alice", "bob", "carol", "dave")}
        same = {"Origin": url}
        alice = people["alice"]  # global admin
        assert alice.request(f"{url}/v2/projects", "POST", {"name": "Payments"}, same)[0] == 201
        team = json.loads(alice.request(f"{url}/v2/teams", "POST", {"name": "payments-team"}, same)[2])
        for who in ("bob", "carol"):
            assert alice.request(f"{url}/v2/teams/{team['team_id']}/members", "POST", {"email": f"{who}@example.com"}, same)[0] == 201
        leads = json.loads(alice.request(f"{url}/v2/teams", "POST", {"name": "payments-leads"}, same)[2])
        alice.request(f"{url}/v2/teams/{leads['team_id']}/members", "POST", {"email": "carol@example.com"}, same)
        assert alice.request(f"{url}/v2/projects/payments/grants", "PUT", {"team_id": team["team_id"], "role": "viewer"}, same)[0] == 200
        assert alice.request(f"{url}/v2/projects/payments/grants", "PUT", {"team_id": leads["team_id"], "role": "admin"}, same)[0] == 200
        status, pay_run, _ = new_run(alice, url, "payments", same)
        assert status == 201
        yield {"url": url, "people": people, "same": same, "pay_run": pay_run, "team": team}
        srv.should_exit = True
        t.join(timeout=10)


def roles(org, who):
    return {p["project_id"]: p["my_role"] for p in json.loads(org["people"][who].request(f"{org['url']}/v2/projects")[2])}


def test_roles_come_from_teams_and_the_project_default(org):
    assert roles(org, "alice") == {"default": "admin", "payments": "admin"}  # global admin
    assert roles(org, "bob") == {"default": "editor", "payments": "viewer"}  # team grant; Default is open to all
    assert roles(org, "carol") == {"default": "editor", "payments": "admin"}  # highest of her two teams
    assert roles(org, "dave") == {"default": "editor"}  # no grant: payments is invisible


def test_what_you_cannot_see_does_not_exist(org):
    url, dave = org["url"], org["people"]["dave"]
    for path in (f"/v1/runs/{org['pay_run']}", f"/v2/runs/{org['pay_run']}/spans", f"/v1/runs/{org['pay_run']}/op-stats"):
        assert dave.request(f"{url}{path}")[0] == 404
    assert dave.request(f"{url}/v2/runs?project=payments")[0] == 404
    assert all(r["project_id"] == "default" for r in json.loads(dave.request(f"{url}/v2/runs")[2])["items"])
    assert org["people"]["bob"].request(f"{url}/v1/runs/{org['pay_run']}")[0] == 200


def test_viewers_read_editors_write_admins_manage(org):
    url, same, p = org["url"], org["same"], org["people"]
    assert new_run(p["bob"], url, "payments", same)[0] == 403  # viewer
    assert p["bob"].request(f"{url}/v2/lab/runs", "POST", {"presets": ["md5", "sha256"], "trials": 1, "duration_s": 0.2,
                                                           "project": "payments"}, same)[0] == 403
    assert new_run(p["carol"], url, "payments", same)[0] == 201  # project admin can write
    # project admin manages grants; a viewer cannot; only global admins create projects and teams
    assert p["carol"].request(f"{url}/v2/projects/payments/grants", "PUT", {"team_id": org["team"]["team_id"], "role": "editor"}, same)[0] == 200
    assert roles(org, "bob")["payments"] == "editor"
    assert new_run(p["bob"], url, "payments", same)[0] == 201
    assert p["bob"].request(f"{url}/v2/projects/payments/grants", "PUT", {"team_id": org["team"]["team_id"], "role": "admin"}, same)[0] == 403
    assert p["carol"].request(f"{url}/v2/projects", "POST", {"name": "Other"}, same)[0] == 403
    assert p["carol"].request(f"{url}/v2/teams", "POST", {"name": "x"}, same)[0] == 403


def test_project_bound_tokens_write_only_where_they_are_bound(org):
    url, same, bob = org["url"], org["same"], org["people"]["bob"]
    created = json.loads(bob.request(f"{url}/v2/tokens", "POST", {"name": "pay-ci", "project": "payments"}, same)[2])
    assert created["project_id"] == "payments"
    bearer = {"Authorization": f"Bearer {created['token']}"}
    anon = Browser()
    status, rid, _ = new_run(anon, url, None, bearer)  # no project named: the token's project
    assert status == 201 and json.loads(anon.request(f"{url}/v1/runs/{rid}", headers=bearer)[2])["project_id"] == "payments"
    assert new_run(anon, url, "default", bearer)[0] == 403  # bound elsewhere
    assert all(r["project_id"] == "payments" for r in json.loads(anon.request(f"{url}/v2/runs", headers=bearer)[2])["items"])
    assert bob.request(f"{url}/v2/tokens", "POST", {"name": "x", "project": "nope"}, same)[0] == 404


def test_otlp_data_lands_in_the_tokens_project(org, monkeypatch):
    import hashlib

    import vayunx
    url, same, carol = org["url"], org["same"], org["people"]["carol"]
    token = json.loads(carol.request(f"{url}/v2/tokens", "POST", {"name": "sdk", "project": "payments"}, same)[2])["token"]
    monkeypatch.setenv("VAYUNX_API_TOKEN", token)
    st = vayunx.init(endpoint=url, service="pay-app", variant="md5", export_interval_s=0.2, gauges=False)
    hashlib.md5(b"x").digest()
    assert vayunx.shutdown(timeout_s=5.0)
    run = json.loads(Browser().request(f"{url}/v1/runs/{st['run_id']}", headers={"Authorization": f"Bearer {token}"})[2])
    assert run["project_id"] == "payments"
    assert org["people"]["dave"].request(f"{url}/v1/runs/{st['run_id']}")[0] == 404
