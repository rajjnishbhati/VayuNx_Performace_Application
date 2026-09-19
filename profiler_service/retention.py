"""Data retention (Phase 4): per-project "keep data for N days". Off everywhere unless a project admin sets it.

A purge removes the project's runs created before now - N days, with their spans, samples, op-stats and Lab
trial rows, then the project's experiments that no longer have any runs. Nothing outside the project is
touched. `purge(..., dry_run=True)` reports the same counts without deleting. The Service runs due purges
in the background every hour (VAYUNX_RETENTION_INTERVAL_S); VAYUNX_RETENTION=off stops them all.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.orm import sessionmaker

from profiler_service.models import Experiment, OpStat, Project, Run, Sample, Span, TrialResult

log = logging.getLogger("vayunx.retention")
MIN_DAYS, MAX_DAYS = 1, 3650
CHILDREN = (("spans", Span), ("samples", Sample), ("op_stats", OpStat), ("trial_results", TrialResult))
BATCH = 500  # runs per delete statement (keeps IN lists and transactions small)


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def enabled() -> bool:
    return os.environ.get("VAYUNX_RETENTION", "on").strip().lower() not in ("off", "0", "false", "no")


def purge(Session: sessionmaker, project_id: str, days: int | None = None, now: datetime | None = None,
          dry_run: bool = False) -> dict:
    """Delete (or with dry_run, count) the project's data older than `days` (default: the project's setting)."""
    now = now or _now()
    with Session() as s:
        project = s.get(Project, project_id)
        if project is None:
            raise ValueError(f"no project {project_id!r}")
        days = days if days is not None else project.retention_days
        if days is None:
            return {"project_id": project_id, "retention_days": None, "dry_run": dry_run, "runs": 0}
        if not MIN_DAYS <= days <= MAX_DAYS:
            raise ValueError(f"retention must be {MIN_DAYS}-{MAX_DAYS} days")
        cutoff = now - timedelta(days=days)
        run_ids = list(s.scalars(select(Run.run_id).where(Run.project_id == project_id, Run.created_at < cutoff)))
        out = {"project_id": project_id, "retention_days": days, "cutoff": cutoff.isoformat() + "Z", "dry_run": dry_run,
               "runs": len(run_ids)}
        for name, model in CHILDREN:
            out[name] = sum(s.scalar(select(func.count()).select_from(model).where(model.run_id.in_(run_ids[i:i + BATCH])))
                            for i in range(0, len(run_ids), BATCH))
        left_behind = select(Run.experiment_id).where(Run.experiment_id.is_not(None), Run.run_id.not_in(run_ids)
                                                      if run_ids else Run.run_id.is_not(None))
        empty = select(Experiment.experiment_id).where(Experiment.project_id == project_id,
                                                       Experiment.created_at < cutoff,
                                                       Experiment.experiment_id.not_in(left_behind),
                                                       Experiment.status.not_in(("queued", "running", "cancelling")))
        exp_ids = list(s.scalars(empty))
        out["experiments"] = len(exp_ids)
        if dry_run:
            return out
        for i in range(0, len(run_ids), BATCH):
            chunk = run_ids[i:i + BATCH]
            for _, model in CHILDREN:
                s.execute(delete(model).where(model.run_id.in_(chunk)))
            s.execute(delete(Run).where(Run.run_id.in_(chunk)))
        if exp_ids:
            s.execute(delete(Experiment).where(Experiment.experiment_id.in_(exp_ids)))
        out["purged_at"] = now.isoformat() + "Z"
        project.retention_last_purge_json = json.dumps(out)
        s.commit()
        log.info("retention: project %s: deleted %s runs older than %s days", project_id, out["runs"], days)
        return out


def run_due_purges(Session: sessionmaker, now: datetime | None = None) -> dict:
    """Purge every project that has a retention setting. Returns {project_id: result}."""
    if not enabled():
        return {}
    with Session() as s:
        projects = list(s.scalars(select(Project.project_id).where(Project.retention_days.is_not(None))))
    results = {}
    for pid in projects:
        try:
            results[pid] = purge(Session, pid, now=now)
        except Exception as exc:  # one bad project must not stop the others
            log.warning("retention: project %s failed: %s", pid, exc)
    return results


class RetentionScheduler(threading.Thread):
    def __init__(self, Session: sessionmaker, interval_s: float | None = None):
        super().__init__(name="vayunx-retention", daemon=True)
        self.Session = Session
        self.interval_s = interval_s or float(os.environ.get("VAYUNX_RETENTION_INTERVAL_S", "3600"))
        self.stop_event = threading.Event()

    def run(self) -> None:
        while not self.stop_event.wait(self.interval_s):
            try:
                run_due_purges(self.Session)
            except Exception as exc:
                log.warning("retention: %s", exc)
