"""Phase 4 data retention: off unless a project admin sets it; preview before deleting; only that project's
runs older than the limit (with everything that belongs to them) are removed."""

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from profiler_service.api import create_app
from profiler_service.auth import AuthConfig
from profiler_service.db import make_engine, make_sessionmaker
from profiler_service.models import Experiment, OpStat, Project, Run, Sample, Span, TrialResult
from profiler_service.retention import purge, run_due_purges
from test_auth import Browser, serve
from test_projects import free_port

NOW = datetime(2026, 9, 19, 12, 0, 0)


def add_run(s, project, age_days, experiment_id=None, with_children=True):
    rid = uuid.uuid4().hex
    t = NOW - timedelta(days=age_days)
    s.add(Run(run_id=rid, service="svc", label="x", phase="baseline", created_at=t, metadata_json="{}",
              project_id=project, experiment_id=experiment_id, source="lab" if experiment_id else "app"))
    s.flush()
    if with_children:
        s.add(Span(run_id=rid, span_id="s1", service="svc", category="general", span_name="a", start_time=t, end_time=t,
                   duration_ms=1.0, attributes_json="{}"))
        s.add(Sample(run_id=rid, service="svc", category="general", metric_name="m", value=1.0, unit="u", timestamp=t))
        s.add(OpStat(run_id=rid, service="svc", category="cryptographic", op_name="hash", attributes_json="{}",
                     interval_start=t, interval_end=t, count=1, sum_ns=1, min_ns=1, max_ns=1, histogram_json="{}"))
    return rid


@pytest.fixture
def db(tmp_path, db_url):
    url = db_url(tmp_path)
    Session = make_sessionmaker(make_engine(url))
    with Session() as s:
        s.add(Project(project_id="pay", name="Pay", default_role=None, created_at=NOW))
        s.add(Experiment(experiment_id="old-exp", source="lab", label="old", status="complete", created_at=NOW - timedelta(days=40),
                         params_json="{}", progress_done=1, progress_total=1, current_json="{}", project_id="pay"))
        s.add(Experiment(experiment_id="new-exp", source="lab", label="new", status="complete", created_at=NOW - timedelta(days=1),
                         params_json="{}", progress_done=1, progress_total=1, current_json="{}", project_id="pay"))
        s.flush()
        ids = {"pay_old": add_run(s, "pay", 40), "pay_new": add_run(s, "pay", 5), "def_old": add_run(s, "default", 400),
               "pay_old_trial": add_run(s, "pay", 40, "old-exp"), "pay_new_trial": add_run(s, "pay", 1, "new-exp")}
        s.add(TrialResult(run_id=ids["pay_old_trial"], experiment_id="old-exp", preset_id="md5", trial_index=0, concurrency=1,
                          ops=1, wall_s=1, ops_per_s=1, cpu_user_s=0, cpu_system_s=0, cpu_s_per_op=0, cores_busy=0,
                          rss_before_bytes=0, peak_rss_bytes=0, peak_rss_method="x", threads_max=1, ctx_switches=0,
                          timer_overhead_ns=0, measure_start=NOW, measure_end=NOW, quiet_json="{}", noisy=0, result_json="{}"))
        s.commit()
    return Session, ids


def count(Session, model, **where):
    with Session() as s:
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return s.scalar(q)


def test_nothing_is_deleted_unless_retention_is_set(db):
    Session, _ = db
    assert run_due_purges(Session, now=NOW) == {}
    assert count(Session, Run) == 5


def test_preview_then_purge_removes_only_old_runs_of_that_project(db):
    Session, ids = db
    with Session() as s:
        s.get(Project, "pay").retention_days = 30
        s.commit()
    preview = purge(Session, "pay", now=NOW, dry_run=True)
    assert preview["runs"] == 2 and preview["spans"] == 2 and preview["samples"] == 2 and preview["op_stats"] == 2
    assert preview["trial_results"] == 1 and preview["experiments"] == 1 and preview["cutoff"].startswith("2026-08-20")
    assert count(Session, Run) == 5  # a preview deletes nothing
    done = run_due_purges(Session, now=NOW)
    assert done["pay"]["runs"] == 2 and "default" not in done
    with Session() as s:
        left = set(s.scalars(select(Run.run_id)))
        assert left == {ids["pay_new"], ids["def_old"], ids["pay_new_trial"]}
        assert s.get(Experiment, "old-exp") is None and s.get(Experiment, "new-exp") is not None
        assert json.loads(s.get(Project, "pay").retention_last_purge_json)["runs"] == 2
    assert count(Session, Span) == 3 and count(Session, TrialResult) == 0


def test_the_global_switch_stops_every_purge(db, monkeypatch):
    Session, _ = db
    with Session() as s:
        s.get(Project, "pay").retention_days = 30
        s.commit()
    monkeypatch.setenv("VAYUNX_RETENTION", "off")
    assert run_due_purges(Session, now=NOW) == {}
    assert count(Session, Run) == 5


def test_only_project_admins_set_retention_and_the_api_previews_first(tmp_path, db_url):
    port = free_port()
    srv, t = serve(create_app(db_url(tmp_path), auth=AuthConfig()), port)
    url, b = f"http://127.0.0.1:{port}", Browser()
    try:
        assert b.request(f"{url}/v2/projects/default/retention", "PUT", {"days": 0})[0] == 422
        assert b.request(f"{url}/v2/projects/default/retention", "PUT", {"days": 99999})[0] == 422
        status, _, text = b.request(f"{url}/v2/projects/default/retention/preview?days=30")
        assert status == 200 and json.loads(text)["runs"] == 0 and json.loads(text)["dry_run"] is True
        status, _, text = b.request(f"{url}/v2/projects/default/retention", "PUT", {"days": 30})
        assert status == 200 and json.loads(text)["retention_days"] == 30
        assert json.loads(b.request(f"{url}/v2/projects")[2])[0]["retention_days"] == 30
        assert json.loads(b.request(f"{url}/v2/projects/default/retention", "PUT", {"days": None})[2])["retention_days"] is None
    finally:
        srv.should_exit = True
        t.join(timeout=10)
