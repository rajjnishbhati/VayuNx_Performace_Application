"""Persistence for Lab experiments. Each trial is stored as:
    Run         (source="lab", variant, experiment_id, trial_index; phase baseline=reference, remediated=candidate)
    OpStat      (the per-operation latency histogram, crypto.* attributes)
    TrialResult (throughput, CPU time, memory, noise check)
    Sample      (machine / process time series captured by the runner while the worker ran)
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from sqlalchemy.orm import sessionmaker

from profiler_service.db import to_utc_naive
from profiler_service.models import Experiment, OpStat, Run, Sample, TrialResult

LAB_SERVICE = "vayunx-lab"
ACTIVE = ("queued", "running")


def _now() -> datetime:
    return to_utc_naive(datetime.now(timezone.utc))


def _dt(iso: str) -> datetime:
    return to_utc_naive(datetime.fromisoformat(iso))


class LabStore:
    def __init__(self, Session: sessionmaker):
        self.Session = Session

    def create_experiment(self, presets: list[str], trials: int, duration_s: float, concurrency: int,
                          reference: str, label: str, env: dict | None = None) -> str:
        exp_id = uuid.uuid4().hex
        with self.Session() as s:
            s.add(Experiment(experiment_id=exp_id, source="lab", label=label, status="queued", created_at=_now(),
                             params_json=json.dumps({"presets": presets, "trials": trials, "duration_s": duration_s,
                                                     "concurrency": concurrency}),
                             reference_preset=reference, progress_done=0, progress_total=trials * len(presets),
                             current_json="{}", env_json=json.dumps(env) if env else None))
            s.commit()
        return exp_id

    def set_status(self, exp_id: str, status: str, error: str | None = None) -> None:
        with self.Session() as s:
            exp = s.get(Experiment, exp_id)
            exp.status = status
            if status == "running" and exp.started_at is None:
                exp.started_at = _now()
            if status in ("complete", "failed", "cancelled"):
                exp.completed_at = _now()
                exp.current_json = "{}"
            if error:
                exp.error = error
            s.commit()

    def status(self, exp_id: str) -> str:
        with self.Session() as s:
            return s.get(Experiment, exp_id).status

    def update_progress(self, exp_id: str, done: int, total: int, current: dict) -> None:
        with self.Session() as s:
            exp = s.get(Experiment, exp_id)
            exp.progress_done, exp.progress_total, exp.current_json = done, total, json.dumps(current)
            s.commit()

    def save_trial(self, *, exp_id: str, preset, trial_index: int, phase: str, result: dict, samples: list[dict],
                   quiet: dict, noisy: bool, other_cores_busy_median: float | None, sampler_overhead_ms: float | None,
                   other_cores_busy_trial: float | None = None) -> str:
        run_id = uuid.uuid4().hex
        env = result.get("env") or {}
        with self.Session() as s:
            s.add(Run(run_id=run_id, service=LAB_SERVICE, label=preset.label, phase=phase, created_at=_now(),
                      completed_at=_now(), variant=preset.label, experiment_id=exp_id, trial_index=trial_index,
                      source="lab", env_json=json.dumps(env),
                      metadata_json=json.dumps({"preset": preset.id, "params": preset.params, "concurrency": result["concurrency"],
                                                "library": result["library"], "runtime": env.get("runtime")})))
            s.flush()
            hist = result["histogram"]
            s.add(OpStat(run_id=run_id, service=LAB_SERVICE, category="cryptographic", op_name="hash",
                         attributes_json=json.dumps({"crypto.algorithm": preset.algorithm, "crypto.params": preset.params,
                                                     "crypto.operation": "hash", "crypto.library": result["library"]}),
                         interval_start=_dt(result["measure_start"]), interval_end=_dt(result["measure_end"]),
                         count=hist["count"], sum_ns=hist["sum_ns"], min_ns=hist["min_ns"], max_ns=hist["max_ns"],
                         p50_ns=result["p50_ns"], p95_ns=result["p95_ns"], p99_ns=result["p99_ns"],
                         histogram_json=json.dumps(hist), sdk_overhead_ns=result.get("timer_overhead_ns")))
            s.add(TrialResult(run_id=run_id, experiment_id=exp_id, preset_id=preset.id, trial_index=trial_index,
                              concurrency=result["concurrency"], ops=result["ops"], wall_s=result["wall_s"],
                              ops_per_s=result["ops_per_s"], cpu_user_s=result["cpu_user_s"],
                              cpu_system_s=result["cpu_system_s"], cpu_s_per_op=result["cpu_s_per_op"],
                              cores_busy=result["cores_busy"], rss_before_bytes=result["rss_before_bytes"],
                              peak_rss_bytes=result["peak_rss_bytes"], peak_rss_method=result["peak_rss_method"],
                              threads_max=result["threads_max"], ctx_switches=result["ctx_switches"],
                              timer_overhead_ns=result["timer_overhead_ns"],
                              measure_start=_dt(result["measure_start"]), measure_end=_dt(result["measure_end"]),
                              quiet_json=json.dumps(quiet), noisy=int(noisy), other_cores_busy_median=other_cores_busy_median,
                              other_cores_busy_trial=other_cores_busy_trial,
                              sampler_overhead_ms=sampler_overhead_ms, result_json=json.dumps(result)))
            s.add_all(Sample(run_id=run_id, service=LAB_SERVICE, category="cryptographic", metric_name=row["metric_name"],
                             value=row["value"], unit=row["unit"], timestamp=to_utc_naive(row["timestamp"]))
                      for row in samples)
            s.commit()
        return run_id
