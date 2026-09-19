"""Copying a database (e.g. the local SQLite profiler.db into Postgres) keeps every row and every result."""

import json

import pytest
from sqlalchemy import func, select

from profiler_service.copy_db import CopyError, copy_database
from profiler_service.db import make_engine, make_sessionmaker
from profiler_service.lab_store import LabStore
from profiler_service.models import OpStat, Run, Sample, Span, TrialResult
from vayunx_lab.runner import ExperimentRunner


@pytest.fixture
def source(tmp_path):
    """A SQLite database with a Lab experiment in it (runs, trials, op-stats, samples)."""
    url = f"sqlite:///{tmp_path}/source.db"
    store = LabStore(make_sessionmaker(make_engine(url)))
    runner = ExperimentRunner(store, ["md5", "sha256"], trials=1, duration_s=0.3, warmup_s=0.05, quiet_max_wait_s=0)
    exp_id = runner.create()
    assert runner.run(exp_id) == "complete"
    return url, exp_id


def counts(url):
    Session = make_sessionmaker(make_engine(url))
    with Session() as s:
        return {m.__tablename__: s.scalar(select(func.count()).select_from(m)) for m in (Run, TrialResult, Span, OpStat, Sample)}


def test_copy_keeps_every_row_and_the_comparison(tmp_path, source, db_url):
    src, exp_id = source
    (tmp_path / "dst").mkdir(exist_ok=True)
    dst = db_url(tmp_path / "dst")
    report = copy_database(src, dst)
    assert counts(src) == counts(dst) and report["rows"]["runs"] == 2 and report["rows"]["samples"] > 0
    a = make_sessionmaker(make_engine(src))
    b = make_sessionmaker(make_engine(dst))
    from profiler_service.api_v2 import _lab_trials
    with a() as sa, b() as sb:
        assert json.dumps([t.__dict__ for t in _lab_trials(sa, exp_id)], default=str, sort_keys=True) == \
            json.dumps([t.__dict__ for t in _lab_trials(sb, exp_id)], default=str, sort_keys=True)
    # new rows after the copy get fresh ids (sequences were moved past the copied ones)
    Session = make_sessionmaker(make_engine(dst))
    with Session() as s:
        run_id = s.scalars(select(Run.run_id)).first()
        s.add(Sample(run_id=run_id, service="x", category="general", metric_name="m", value=1.0, unit="u",
                     timestamp=s.scalars(select(Sample.timestamp)).first()))
        s.commit()


def test_copy_refuses_a_target_that_already_has_data(tmp_path, source, db_url):
    src, _ = source
    (tmp_path / "dst").mkdir(exist_ok=True)
    dst = db_url(tmp_path / "dst")
    copy_database(src, dst)
    with pytest.raises(CopyError, match="not empty"):
        copy_database(src, dst)


def test_copy_never_writes_to_the_source(tmp_path, source, db_url):
    src, _ = source
    before = counts(src)
    (tmp_path / "dst").mkdir(exist_ok=True)
    copy_database(src, db_url(tmp_path / "dst"))
    assert counts(src) == before
