"""/v2 API (spec D): presets, Lab experiments with progress, compare (Lab and app), time series, search.

All /v1 endpoints keep working unchanged. Errors are JSON {"detail": {"error": <plain cause>, "fix": <what to do>}}.
"""

import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from profiler_service.compare_v2 import TrialInput, compare
from profiler_service.db import iso_utc
from profiler_service.lab_jobs import Busy
from profiler_service.models import Experiment, OpStat, Run, Sample, Span, TrialResult
from profiler_service.runs_view import runs_out_v2
from vayunx_lab.presets import PRESETS, PresetError, get_preset
from vayunx_lab.security import note_for_algorithm, security_note
from vayunx_lab.worker import ALLOWED_CONCURRENCY
from vayunx_profiler_sdk.histogram import LatencyHistogram

router = APIRouter(prefix="/v2", tags=["v2"])
TIMESERIES_METRICS = ("proc_cores_busy", "proc_rss_mib", "proc_threads", "machine_cpu_pct",
                      "machine_other_cores_busy", "machine_mem_available_mib")
TRIAL_OVERHEAD_S = 2.5  # rough per-trial extra time (process start, warm-up, quiet check) used only for ETAs


def get_session(request: Request):
    session: Session = request.app.state.sessionmaker()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def problem(status: int, error: str, fix: str) -> HTTPException:
    return HTTPException(status, {"error": error, "fix": fix})


# ----------------------------------------------------------------------------- presets


@router.get("/presets")
def list_presets() -> list[dict]:
    return [{**p.public(), "security": security_note(p)} for p in PRESETS.values()]


# ----------------------------------------------------------------------------- lab runs


class LabRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    presets: list[str] = Field(description="two or more built-in preset ids")
    reference: str | None = Field(default=None, description="preset to compare against (default: the first)")
    trials: int = Field(default=5, ge=1, le=20)
    duration_s: float = Field(default=10.0, ge=0.2, le=60.0)
    concurrency: int = 1
    warmup_s: float = Field(default=1.0, ge=0.0, le=5.0)


@router.post("/lab/runs", status_code=202)
def start_lab_run(body: LabRunIn, request: Request) -> dict:
    if len(set(body.presets)) < 2:
        raise problem(422, "Pick at least two different algorithms to compare.",
                      "Choose two or more presets from GET /v2/presets.")
    for p in body.presets:
        try:
            get_preset(p)
        except PresetError:
            raise problem(422, f"unknown preset {p!r}.", f"Use one of: {', '.join(PRESETS)}.") from None
    if body.reference is not None and body.reference not in body.presets:
        raise problem(422, "The reference must be one of the chosen presets.",
                      "Set reference to one of the presets you picked, or leave it out to use the first.")
    if body.concurrency not in ALLOWED_CONCURRENCY:
        raise problem(422, f"concurrency {body.concurrency} is not supported.",
                      f"Use one of {', '.join(map(str, ALLOWED_CONCURRENCY))}.")
    try:
        exp_id = request.app.state.lab_jobs.start(body.presets, trials=body.trials, duration_s=body.duration_s,
                                                  concurrency=body.concurrency, reference=body.reference,
                                                  warmup_s=body.warmup_s)
    except Busy as exc:
        raise problem(409, f"A Lab experiment is already running ({exc.experiment_id}).",
                      f"Wait for it to finish, or cancel it with POST /v2/experiments/{exc.experiment_id}/cancel.") from None
    except (ValueError, PresetError) as exc:
        raise problem(422, str(exc), "Check the parameters against GET /v2/presets and the API docs.") from None
    return {"experiment_id": exp_id, "status": "queued"}


# ----------------------------------------------------------------------------- experiments


def _load_experiment(session: Session, exp_id: str) -> Experiment:
    exp = session.get(Experiment, exp_id)
    if exp is None:
        raise problem(404, f"No experiment with id {exp_id!r}.", "Pick an experiment from GET /v2/experiments.")
    return exp


def experiment_out(exp: Experiment) -> dict:
    params = json.loads(exp.params_json)
    done, total = exp.progress_done, exp.progress_total
    eta = None
    if exp.status in ("queued", "running") and total:
        if done and exp.started_at:
            elapsed = (datetime.now(timezone.utc).replace(tzinfo=None) - exp.started_at).total_seconds()
            eta = elapsed / done * (total - done)
        else:
            eta = (total - done) * (params.get("duration_s", 10.0) + TRIAL_OVERHEAD_S)
    return {"experiment_id": exp.experiment_id, "source": exp.source, "label": exp.label, "status": exp.status,
            "created_at": iso_utc(exp.created_at), "started_at": iso_utc(exp.started_at),
            "completed_at": iso_utc(exp.completed_at), "params": params, "reference_preset": exp.reference_preset,
            "progress": {"done": done, "total": total, "percent": round(100 * done / total) if total else 0,
                         "current": json.loads(exp.current_json or "{}"), "eta_s": eta},
            "error": exp.error, "env": json.loads(exp.env_json) if exp.env_json else None}


@router.get("/experiments")
def list_experiments(session: SessionDep, q: str | None = None, source: str | None = None, status: str | None = None,
                     limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict:
    query = select(Experiment)
    if q:
        query = query.where(Experiment.label.ilike(f"%{q}%"))
    if source:
        query = query.where(Experiment.source == source)
    if status:
        query = query.where(Experiment.status == status)
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    items = session.scalars(query.order_by(Experiment.created_at.desc()).limit(limit).offset(offset)).all()
    return {"items": [experiment_out(e) for e in items], "total": total, "limit": limit, "offset": offset}


@router.get("/experiments/{exp_id}")
def get_experiment(exp_id: str, session: SessionDep) -> dict:
    return experiment_out(_load_experiment(session, exp_id))


@router.post("/experiments/{exp_id}/cancel", status_code=202)
def cancel_experiment(exp_id: str, session: SessionDep, request: Request) -> dict:
    exp = _load_experiment(session, exp_id)
    if exp.status not in ("queued", "running"):
        raise problem(409, f"The experiment is already {exp.status}.", "Only queued or running experiments can be cancelled.")
    if not request.app.state.lab_jobs.cancel(exp_id):
        exp.status = "cancelling"  # running in another process (e.g. the CLI): it checks this between trials
        session.commit()
    return {"experiment_id": exp_id, "status": "cancelling"}


@router.get("/experiments/{exp_id}/timeseries")
def experiment_timeseries(exp_id: str, session: SessionDep) -> dict:
    """Machine view data: one series per metric on a shared time axis, plus regions where crypto ran."""
    _load_experiment(session, exp_id)
    runs = session.scalars(select(Run).where(Run.experiment_id == exp_id).order_by(Run.created_at)).all()
    trials = {t.run_id: t for t in session.scalars(select(TrialResult).where(TrialResult.experiment_id == exp_id))}
    samples = session.scalars(select(Sample).where(Sample.run_id.in_([r.run_id for r in runs]),
                                                   Sample.metric_name.in_(TIMESERIES_METRICS))
                              .order_by(Sample.timestamp)).all()
    t0 = samples[0].timestamp if samples else (runs[0].created_at if runs else None)
    rel = (lambda dt: (dt - t0).total_seconds()) if t0 else (lambda dt: 0.0)
    preset_of = {r.run_id: (trials[r.run_id].preset_id if r.run_id in trials else r.variant) for r in runs}
    metrics: dict = {}
    for s in samples:
        m = metrics.setdefault(s.metric_name, {"unit": s.unit, "points": []})
        m["points"].append([round(rel(s.timestamp), 4), s.value, preset_of.get(s.run_id)])
    regions = [{"start_s": round(rel(t.measure_start), 4), "end_s": round(rel(t.measure_end), 4), "preset": t.preset_id,
                "variant": get_preset(t.preset_id).label, "trial": t.trial_index + 1, "noisy": bool(t.noisy)}
               for t in sorted(trials.values(), key=lambda t: t.measure_start)]
    return {"t0": iso_utc(t0), "metrics": metrics, "regions": regions,
            "note": "Shaded regions are the measured part of each trial; gaps are process start-up and warm-up."}


# ----------------------------------------------------------------------------- compare


def _lab_trials(session: Session, exp_id: str) -> list[TrialInput]:
    rows = session.execute(select(TrialResult, OpStat).join(OpStat, OpStat.run_id == TrialResult.run_id)
                           .where(TrialResult.experiment_id == exp_id).order_by(TrialResult.trial_index)).all()
    out = []
    for tr, op in rows:
        preset = get_preset(tr.preset_id)
        out.append(TrialInput(variant_key=preset.id, variant_label=preset.label, trial_index=tr.trial_index,
                              histogram=json.loads(op.histogram_json), wall_s=tr.wall_s, cpu_s_per_op=tr.cpu_s_per_op,
                              cores_busy=tr.cores_busy, peak_rss_bytes=tr.peak_rss_bytes,
                              rss_before_bytes=tr.rss_before_bytes, noisy=bool(tr.noisy),
                              timer_overhead_ns=tr.timer_overhead_ns, concurrency=tr.concurrency,
                              family=preset.family, security=security_note(preset),
                              exact_percentiles=json.loads(tr.result_json).get("exact_percentiles")))
    return out


MIN_CPU_SPAN_COVERAGE = 0.5  # per-call CPU is used only if CPU-timed spans cover at least half the calls


def _op_key(attrs: dict, fallback: str) -> str:
    """Crypto recorded inside vayunx.span/measure("login") -> "span:login" (every crypto call in that code
    path, whatever its operation: MD5 recomputes a hash at login while Argon2id verifies); otherwise the
    operation itself -> "op:hash"."""
    scope = attrs.get("vayunx.scope")
    return f"span:{scope}" if scope else f"op:{attrs.get('crypto.operation') or fallback}"


def _label(key: str) -> str:
    return key.split(":", 1)[1]


def _median(xs: list[float]) -> float | None:
    xs = sorted(xs)
    if not xs:
        return None
    m = len(xs) // 2
    return xs[m] if len(xs) % 2 else (xs[m - 1] + xs[m]) / 2


def _part_name(attrs: dict, fallback: str) -> str:
    return f"{attrs.get('crypto.operation') or fallback} {attrs.get('crypto.algorithm') or ''}".strip()


def _app_trials(session: Session, runs: list[Run]) -> tuple[list[TrialInput], dict]:
    """Each app run is one trial of its variant (run.variant or run.label).

    Matching: crypto recorded inside a span the user named (SDK attribute vayunx.scope) is matched by that
    name; other data by crypto.operation (or the op/span name). Named code paths shared by all runs win over
    unscoped operations - they are what the user labelled, not incidental hashing elsewhere in the app.

    CPU per call: the median `vayunx.cpu_time_ms` of the matching crypto spans (per-call thread CPU from the
    SDK), used only when those spans cover at least half of the calls. Fast calls are not CPU-timed one by
    one, so their CPU is reported as not measured - never derived from whole-process CPU, which includes
    request handling. Memory per call is not measured for apps (process RSS growth is not per-call memory);
    peak RSS is still reported."""
    per_run: dict[str, dict[str, dict]] = {}
    cpu_ms: dict[str, dict[str, list[float]]] = {}
    for run in runs:
        ops: dict[str, dict] = {}
        for row in session.scalars(select(OpStat).where(OpStat.run_id == run.run_id)):
            attrs = json.loads(row.attributes_json)
            entry = ops.setdefault(_op_key(attrs, row.op_name), {"hist": LatencyHistogram(), "parts": {}})
            h = LatencyHistogram.from_dict(json.loads(row.histogram_json))
            entry["hist"].merge(h)
            part = entry["parts"].setdefault(_part_name(attrs, row.op_name), {"ns": 0, "attrs": attrs})
            part["ns"] += h.sum_ns
        spans = session.scalars(select(Span).where(Span.run_id == run.run_id, Span.category == "cryptographic")).all()
        cpu_ms[run.run_id] = defaultdict(list)
        for sp in spans:
            attrs = json.loads(sp.attributes_json)
            if isinstance(attrs.get("vayunx.cpu_time_ms"), (int, float)):
                cpu_ms[run.run_id][_op_key(attrs, sp.span_name)].append(float(attrs["vayunx.cpu_time_ms"]))
        if not ops:  # no op-stats (older SDKs): build histograms from cryptographic spans
            for sp in spans:
                attrs = json.loads(sp.attributes_json)
                entry = ops.setdefault(_op_key(attrs, sp.span_name), {"hist": LatencyHistogram(), "parts": {}})
                entry["hist"].record(int(sp.duration_ms * 1e6))
                part = entry["parts"].setdefault(_part_name(attrs, sp.span_name), {"ns": 0, "attrs": attrs})
                part["ns"] += int(sp.duration_ms * 1e6)
        per_run[run.run_id] = ops
    common = set.intersection(*(set(o) for o in per_run.values())) if per_run else set()
    if not common:
        raise problem(409, "These runs have no crypto operation in common.",
                      'Pick runs that record the same operation, or wrap the code path in vayunx.span("login") '
                      "in every variant.")
    scoped = {k for k in common if k.startswith("span:")}
    totals = {k: sum(per_run[r][k]["hist"].sum_ns for r in per_run) for k in (scoped or common)}
    key = max(totals, key=totals.get)
    trials, detail = [], {}
    for i, run in enumerate(runs):
        entry = per_run[run.run_id][key]
        attrs = max(entry["parts"].values(), key=lambda part: part["ns"])["attrs"]
        variant = run.variant or run.label
        detail[variant] = sorted(entry["parts"], key=lambda k: -entry["parts"][k]["ns"])
        samples = session.scalars(select(Sample).where(Sample.run_id == run.run_id).order_by(Sample.timestamp)).all()
        by = defaultdict(list)
        for s in samples:
            by[s.metric_name].append(s)
        rss = [s.value for s in by.get("proc_peak_rss_mib", []) or by.get("proc_rss_mib", []) or by.get("memory_mb", [])]
        cores = [s.value for s in by.get("proc_cores_busy", []) if s.category == "cryptographic"] or \
                [s.value / 100 for s in by.get("cpu_pct", [])]
        hist = entry["hist"]
        cpu_list = cpu_ms[run.run_id].get(key, [])
        covered = bool(hist.count) and len(cpu_list) >= MIN_CPU_SPAN_COVERAGE * hist.count
        other = [s.value for s in by.get("machine_other_cores_busy", []) if s.category == "cryptographic"]
        sec = note_for_algorithm(attrs.get("crypto.algorithm"), attrs.get("crypto.params"))
        trials.append(TrialInput(
            variant_key=variant, variant_label=variant, trial_index=i, histogram=hist.to_dict(),
            cpu_s_per_op=_median(cpu_list) / 1000 if covered else None,
            cores_busy=sorted(cores)[len(cores) // 2] if cores else None,
            peak_rss_bytes=int(max(rss) * 1024 * 1024) if rss else None, rss_before_bytes=None,
            noisy=bool(other) and sorted(other)[len(other) // 2] > 0.5,
            family=("password-hash" if sec and sec["safe_for_passwords"] else "fast-hash" if sec else None),
            security=sec, peak_rss_approximate=True))
    info = {"operation": _label(key), "operation_kind": "span" if key.startswith("span:") else "operation",
            "operation_detail": detail, "other_operations": sorted(_label(k) for k in common - {key}),
            "measurement_notes": [
                "CPU per call comes from per-call thread CPU on crypto spans (calls at least slow_ms long); faster "
                "calls are not CPU-timed one by one, so their CPU per call is shown as not measured.",
                "Memory per call is not measured for apps; peak process memory is shown instead."]}
    return trials, info


@router.get("/compare")
def compare_v2(session: SessionDep, experiment_id: str | None = None, run_ids: str | None = None,
               reference: str | None = None, rate: float = Query(100.0, gt=0, le=1e7),
               cores: int | None = Query(None, ge=1, le=4096)) -> dict:
    if not experiment_id and not run_ids:
        raise problem(422, "Nothing to compare.",
                      "Pass experiment_id=<id> for a Lab experiment, or run_ids=<id>,<id> for app runs.")
    if experiment_id:
        exp = _load_experiment(session, experiment_id)
        trials = _lab_trials(session, experiment_id)
        if len({t.variant_key for t in trials}) < 2:
            raise problem(409, "This experiment does not have finished trials for two algorithms yet.",
                          "Wait for the experiment to progress, then try again.")
        env = json.loads(exp.env_json) if exp.env_json else {}
        ref = reference or exp.reference_preset
        try:
            result = compare(trials, ref, rate_per_s=rate, cores_total=cores or env.get("cpu_count_logical") or os.cpu_count() or 1,
                             source="lab", op_noun="hash")
        except ValueError as exc:
            raise problem(422, str(exc), "Choose a reference that is one of the experiment's presets.") from None
        result["experiment"] = experiment_out(exp)
        return result

    ids = [r for r in run_ids.split(",") if r]
    runs = [session.get(Run, r) for r in ids]
    missing = [rid for rid, run in zip(ids, runs) if run is None]
    if missing:
        raise problem(404, f"Unknown run id(s): {', '.join(missing)}.", "Pick runs from GET /v2/runs.")
    trials, info = _app_trials(session, runs)
    ref = reference or trials[0].variant_key
    cores_total = cores or (json.loads(runs[0].metadata_json).get("cpu_count") or os.cpu_count() or 1)
    try:
        result = compare(trials, ref, rate_per_s=rate, cores_total=cores_total, source="app", op_noun="call")
    except ValueError as exc:
        raise problem(422, str(exc), "Select runs from at least two variants (different labels or VAYUNX_VARIANT).") from None
    result.update(info, runs=runs_out_v2(session, runs))
    return result


# ----------------------------------------------------------------------------- run detail


def _load_run(session: Session, run_id: str) -> Run:
    run = session.get(Run, run_id)
    if run is None:
        raise problem(404, f"No run with id {run_id!r}.", "Pick a run from GET /v2/runs.")
    return run


@router.get("/runs/{run_id}/spans")
def run_spans(run_id: str, session: SessionDep, limit: int = Query(5000, ge=1, le=50000)) -> list[dict]:
    _load_run(session, run_id)
    rows = session.scalars(select(Span).where(Span.run_id == run_id).order_by(Span.start_time).limit(limit))
    return [{"span_id": s.span_id, "parent_span_id": s.parent_span_id, "span_name": s.span_name, "category": s.category,
             "start_time": iso_utc(s.start_time), "end_time": iso_utc(s.end_time), "duration_ms": s.duration_ms,
             "attributes": json.loads(s.attributes_json)} for s in rows]


@router.get("/runs/{run_id}/samples")
def run_samples(run_id: str, session: SessionDep, metric: str | None = None,
                limit: int = Query(20000, ge=1, le=200000)) -> list[dict]:
    _load_run(session, run_id)
    q = select(Sample).where(Sample.run_id == run_id)
    if metric:
        q = q.where(Sample.metric_name == metric)
    return [{"metric_name": s.metric_name, "category": s.category, "value": s.value, "unit": s.unit,
             "timestamp": iso_utc(s.timestamp)} for s in session.scalars(q.order_by(Sample.timestamp).limit(limit))]


# ----------------------------------------------------------------------------- runs search


@router.get("/runs")
def search_runs(session: SessionDep, q: str | None = None, service: str | None = None, algorithm: str | None = None,
                source: str | None = None, date_from: datetime | None = None, date_to: datetime | None = None,
                limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict:
    query = select(Run)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Run.label.ilike(like), Run.variant.ilike(like), Run.service.ilike(like)))
    if service:
        query = query.where(Run.service == service)
    if algorithm:
        query = query.where(or_(Run.variant.ilike(f"%{algorithm}%"), Run.label.ilike(f"%{algorithm}%")))
    if source:
        query = query.where(Run.source == source)
    if date_from:
        query = query.where(Run.created_at >= date_from.replace(tzinfo=None))
    if date_to:
        query = query.where(Run.created_at <= date_to.replace(tzinfo=None))
    total = session.scalar(select(func.count()).select_from(query.subquery()))
    runs = list(session.scalars(query.order_by(Run.created_at.desc()).limit(limit).offset(offset)))
    return {"items": runs_out_v2(session, runs), "total": total, "limit": limit, "offset": offset}
