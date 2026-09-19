"""Run serialisation shared by v1 and v2: counts come from grouped queries (no N+1)."""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from profiler_service.db import iso_utc
from profiler_service.models import OpStat, Run, Sample, Span
from profiler_service.schemas import RunOut


def counts_for(session: Session, run_ids: list[str]) -> dict[str, dict[str, int]]:
    out = {rid: {"span_count": 0, "sample_count": 0, "op_stat_count": 0} for rid in run_ids}
    if not run_ids:
        return out
    for model, key in ((Span, "span_count"), (Sample, "sample_count"), (OpStat, "op_stat_count")):
        q = select(model.run_id, func.count()).where(model.run_id.in_(run_ids)).group_by(model.run_id)
        for rid, n in session.execute(q):
            out[rid][key] = n
    return out


def runs_out(session: Session, runs: list[Run]) -> list[dict]:
    counts = counts_for(session, [r.run_id for r in runs])
    return [RunOut(run_id=r.run_id, service=r.service, label=r.label, phase=r.phase,
                   created_at=iso_utc(r.created_at), completed_at=iso_utc(r.completed_at),
                   metadata=json.loads(r.metadata_json), project_id=r.project_id or "default",
                   **counts[r.run_id]).model_dump() for r in runs]


def runs_out_v2(session: Session, runs: list[Run]) -> list[dict]:
    base = runs_out(session, runs)
    for item, r in zip(base, runs):
        item.update(variant=r.variant, experiment_id=r.experiment_id, trial_index=r.trial_index, source=r.source or "app")
    return base
