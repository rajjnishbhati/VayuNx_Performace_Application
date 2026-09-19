"""Lab runner: interleaved trials in fresh subprocesses, quiet-machine check, storage."""

import json
import sqlite3
import threading

from sqlalchemy import inspect, select

from profiler_service.db import make_engine, make_sessionmaker
from profiler_service.lab_store import LabStore
from profiler_service.models import Experiment, OpStat, Run, Sample, TrialResult
from vayunx_lab.runner import ExperimentRunner, quiet_check, schedule


def test_schedule_interleaves_with_alternating_order():
    assert schedule(["a", "b"], 3) == [(0, "a"), (0, "b"), (1, "b"), (1, "a"), (2, "a"), (2, "b")]
    assert schedule(["a", "b", "c"], 2) == [(0, "a"), (0, "b"), (0, "c"), (1, "c"), (1, "b"), (1, "a")]


def test_quiet_check_waits_for_a_quiet_machine():
    readings = iter([80.0, 60.0, 10.0])
    waited = []
    result = quiet_check(read_cpu=lambda: next(readings), threshold_pct=25.0, max_wait_s=5.0, sleep=waited.append, step_s=1.0)
    assert result["quiet"] is True and result["machine_cpu_pct"] == 10.0 and result["waited_s"] == 2.0


def test_quiet_check_flags_noise_instead_of_blocking():
    result = quiet_check(read_cpu=lambda: 90.0, threshold_pct=25.0, max_wait_s=3.0, sleep=lambda s: None, step_s=1.0)
    assert result["quiet"] is False and result["waited_s"] == 3.0 and result["machine_cpu_pct"] == 90.0


def make_store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'lab.db'}")
    return LabStore(make_sessionmaker(engine)), make_sessionmaker(engine)


def test_runner_end_to_end_stores_runs_histograms_trials_and_machine_series(tmp_path):
    store, Session = make_store(tmp_path)
    runner = ExperimentRunner(store, ["md5", "sha256"], trials=2, duration_s=0.4, warmup_s=0.05,
                              sample_interval_s=0.05, quiet_threshold_pct=100.0)
    exp_id = runner.create()
    runner.run(exp_id)
    with Session() as s:
        exp = s.get(Experiment, exp_id)
        assert exp.status == "complete" and exp.progress_done == exp.progress_total == 4
        assert json.loads(exp.params_json)["presets"] == ["md5", "sha256"] and exp.reference_preset == "md5"
        runs = s.scalars(select(Run).where(Run.experiment_id == exp_id).order_by(Run.created_at)).all()
        assert [(r.trial_index, r.variant) for r in runs] == [(0, "MD5"), (0, "SHA-256"), (1, "SHA-256"), (1, "MD5")]
        assert {r.source for r in runs} == {"lab"} and all(r.completed_at for r in runs)
        assert {r.phase for r in runs if r.variant == "MD5"} == {"baseline"}  # reference maps to v1 baseline
        assert {r.phase for r in runs if r.variant == "SHA-256"} == {"remediated"}
        for r in runs:
            op = s.scalars(select(OpStat).where(OpStat.run_id == r.run_id)).one()
            trial = s.get(TrialResult, r.run_id)
            assert op.count == trial.ops > 0 and json.loads(op.histogram_json)["count"] == op.count
            assert trial.cpu_s_per_op > 0 and trial.peak_rss_bytes > 0 and trial.timer_overhead_ns > 0
            assert json.loads(trial.quiet_json)["threshold_pct"] == 100.0
            names = {m for (m,) in s.execute(select(Sample.metric_name).where(Sample.run_id == r.run_id).distinct())}
            assert {"proc_cores_busy", "proc_rss_mib", "machine_cpu_pct", "machine_other_cores_busy"} <= names
        assert json.loads(runs[0].env_json)["cpu_model"]


def test_runner_can_be_cancelled_between_trials(tmp_path):
    store, Session = make_store(tmp_path)
    cancel = threading.Event()
    runner = ExperimentRunner(store, ["md5", "sha256"], trials=3, duration_s=0.2, warmup_s=0.0,
                              quiet_threshold_pct=100.0, cancel_event=cancel,
                              on_trial_done=lambda done, total: cancel.set() if done == 1 else None)
    exp_id = runner.create()
    runner.run(exp_id)
    with Session() as s:
        exp = s.get(Experiment, exp_id)
        assert exp.status == "cancelled" and exp.progress_done == 1


def test_runner_reports_worker_failure_plainly(tmp_path):
    store, Session = make_store(tmp_path)
    runner = ExperimentRunner(store, ["md5"], trials=1, duration_s=0.2, quiet_threshold_pct=100.0,
                              python="definitely-not-a-python-executable")
    exp_id = runner.create()
    runner.run(exp_id)
    with Session() as s:
        exp = s.get(Experiment, exp_id)
        assert exp.status == "failed" and "could not start" in exp.error


def test_existing_database_gains_new_columns_without_losing_rows(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE runs (run_id VARCHAR(64) PRIMARY KEY, service VARCHAR(128), label VARCHAR(256), "
                "phase VARCHAR(16), created_at DATETIME, completed_at DATETIME, metadata_json TEXT)")
    con.execute("INSERT INTO runs VALUES ('old1', 'svc', 'l', 'baseline', '2026-09-17 10:00:00', NULL, '{}')")
    con.commit()
    con.close()
    engine = make_engine(f"sqlite:///{path}")
    cols = {c["name"] for c in inspect(engine).get_columns("runs")}
    assert {"variant", "experiment_id", "trial_index", "source", "env_json"} <= cols
    with make_sessionmaker(engine)() as s:
        old = s.get(Run, "old1")
        assert old.label == "l" and old.source == "app"
