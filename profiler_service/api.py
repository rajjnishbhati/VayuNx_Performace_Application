"""FastAPI app: ingestion, runs, comparison (JSON) and report (HTML)."""

# No `from __future__ import annotations` here: FastAPI must resolve the Annotated dependency types at runtime.
import json
import math
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from profiler_service import WIRE_SCHEMA_VERSION, __version__, config
from profiler_service.comparison import SampleRec, SpanRec, build_report
from profiler_service.access import Access, access_for, load_run, scope_query
from profiler_service.access import router as access_router
from profiler_service.api_v2 import router as v2_router
from profiler_service.auth import AuthConfig
from profiler_service.auth import install as install_auth
from profiler_service.db import iso_utc, make_engine, make_sessionmaker, to_utc_naive
from profiler_service.lab_jobs import LabJobs
from profiler_service.otlp import router as otlp_router
from profiler_service.retention import RetentionScheduler
from profiler_service.models import OpStat, Run, Sample, Span
from profiler_service.report_html import render_error, render_index, render_report
from profiler_service.runs_view import runs_out
from profiler_service.schemas import IngestResult, OpStatsIn, RunComplete, RunCreate, RunOut, SampleIn, SpanIn


def get_session(request: Request):
    session: Session = request.app.state.sessionmaker()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def get_access(request: Request, session: SessionDep) -> Access:
    return access_for(request, session)


AccessDep = Annotated[Access, Depends(get_access)]


def create_app(db_url: str | None = None, auth: AuthConfig | None = None) -> FastAPI:
    db_url = db_url or config.DB_URL
    auth = auth or AuthConfig.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = make_engine(db_url)
        app.state.sessionmaker = make_sessionmaker(app.state.engine)
        app.state.lab_jobs = LabJobs(app.state.sessionmaker)
        app.state.lab_jobs.recover()  # experiments left "running" by a previous process are marked failed
        app.state.retention = RetentionScheduler(app.state.sessionmaker)  # purges only projects that opted in
        app.state.retention.start()
        yield
        app.state.retention.stop_event.set()
        app.state.lab_jobs.cancel_current()
        app.state.engine.dispose()

    app = FastAPI(title="VAYUNX Profiler Service", version=__version__, lifespan=lifespan,
                  description="Language-agnostic span/sample ingestion, baseline-vs-remediated comparison and flame-graph reports. "
                              "Sign-in (OIDC) and API tokens when VAYUNX_AUTH=oidc; open otherwise.")
    app.include_router(v2_router)
    app.include_router(access_router)  # /v2/projects, /v2/teams, /v2/users
    app.include_router(otlp_router)  # OTLP/HTTP: POST /v1/traces, POST /v1/metrics

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        if request.url.path in HTML_ROUTES:  # people, not programs: plain page with a way back
            problems = "; ".join(f"{'.'.join(str(p) for p in e.get('loc', [])[1:])}: {e.get('msg')}" for e in exc.errors())
            return html_error(422, f"The request is incomplete or invalid ({problems}).")
        # Default handler echoes the offending input; NaN/Infinity would make that response itself
        # unserialisable (HTTP 500). Replace non-finite floats so bad input always gets a clean 422.
        def safe(v):
            if isinstance(v, float) and not math.isfinite(v):
                return str(v)
            if isinstance(v, dict):
                return {k: safe(x) for k, x in v.items()}
            if isinstance(v, (list, tuple)):
                return [safe(x) for x in v]
            return v
        return JSONResponse(status_code=422, content={"detail": safe(jsonable_encoder(exc.errors()))})

    def run_out(session: Session, run: Run) -> dict:
        return runs_out(session, [run])[0]

    def check_batch(session: Session, access: Access, items: list, kind: str) -> None:
        if not items:
            raise HTTPException(422, f"empty {kind} batch")
        if len(items) > config.MAX_BATCH_ITEMS:
            raise HTTPException(413, f"batch of {len(items)} {kind}s exceeds limit of {config.MAX_BATCH_ITEMS}")
        runs: dict[str, Run] = {}
        for i, item in enumerate(items):
            run = runs.get(item.run_id) or session.get(Run, item.run_id)
            if run is None or not access.can(run.project_id):
                raise HTTPException(404, {"error": f"run {item.run_id!r} not found", "index": i})
            if not access.can(run.project_id, "editor"):
                raise HTTPException(403, {"error": f"you need the editor role on project {run.project_id!r}", "index": i})
            runs[item.run_id] = run
            if run.completed_at is not None:
                raise HTTPException(409, {"error": f"run {run.run_id!r} is already complete; no further data accepted", "index": i})
            if item.service != run.service:
                raise HTTPException(422, {"error": f"service {item.service!r} does not match run service {run.service!r}", "index": i})

    # ------------------------------------------------------------------ health / runs

    @app.get("/healthz")
    def healthz():
        return {"status": "ok", "service": "vayunx-profiler-service", "version": __version__, "wire_schema_version": WIRE_SCHEMA_VERSION}

    @app.post("/v1/runs", status_code=201)
    def create_run(body: RunCreate, session: SessionDep, access: AccessDep) -> RunOut:
        project_id = access.write_project(body.project, session)
        run_id = body.run_id or uuid.uuid4().hex
        if session.get(Run, run_id) is not None:
            raise HTTPException(409, f"run {run_id!r} already exists")
        run = Run(run_id=run_id, service=body.service, label=body.label, phase=body.phase,
                  created_at=to_utc_naive(datetime.now(timezone.utc)), metadata_json=json.dumps(body.metadata),
                  project_id=project_id)
        session.add(run)
        session.commit()
        return run_out(session, run)

    @app.get("/v1/runs")
    def list_runs(session: SessionDep, access: AccessDep, service: str | None = None, phase: str | None = None,
                  project: str | None = None, limit: int = Query(200, ge=1, le=1000),
                  offset: int = Query(0, ge=0)) -> list[RunOut]:
        q = scope_query(select(Run), access, Run, project, session).order_by(Run.created_at.desc()).limit(limit).offset(offset)
        if service:
            q = q.where(Run.service == service)
        if phase:
            q = q.where(Run.phase == phase)
        return runs_out(session, list(session.scalars(q)))

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, session: SessionDep, access: AccessDep) -> RunOut:
        return run_out(session, load_run(session, access, run_id))

    @app.post("/v1/runs/{run_id}/complete")
    def complete_run(run_id: str, session: SessionDep, access: AccessDep, body: RunComplete | None = None) -> RunOut:
        run = load_run(session, access, run_id, "editor")
        if run.completed_at is not None:
            raise HTTPException(409, f"run {run_id!r} is already complete")
        completed = body.completed_at if body and body.completed_at else datetime.now(timezone.utc)
        run.completed_at = to_utc_naive(completed)
        session.commit()
        return run_out(session, run)

    # ------------------------------------------------------------------ ingestion (single object or array)

    @app.post("/v1/spans", status_code=201)
    def ingest_spans(body: Annotated[SpanIn | list[SpanIn], Body()], session: SessionDep, access: AccessDep) -> IngestResult:
        items = body if isinstance(body, list) else [body]
        check_batch(session, access, items, "span")
        seen = set()
        for i, s in enumerate(items):
            if (s.run_id, s.span_id) in seen:
                raise HTTPException(409, {"error": f"duplicate span_id {s.span_id!r} within batch", "index": i})
            seen.add((s.run_id, s.span_id))
        session.add_all(Span(run_id=s.run_id, span_id=s.span_id, parent_span_id=s.parent_span_id, service=s.service,
                             category=s.category, span_name=s.span_name, start_time=to_utc_naive(s.start_time),
                             end_time=to_utc_naive(s.end_time), duration_ms=s.duration_ms,
                             attributes_json=json.dumps(s.attributes)) for s in items)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
            raise HTTPException(409, "a span_id in this batch already exists for its run; batch rejected (nothing stored)")
        return IngestResult(accepted=len(items))

    @app.post("/v1/samples", status_code=201)
    def ingest_samples(body: Annotated[SampleIn | list[SampleIn], Body()], session: SessionDep, access: AccessDep) -> IngestResult:
        items = body if isinstance(body, list) else [body]
        check_batch(session, access, items, "sample")
        session.add_all(Sample(run_id=s.run_id, service=s.service, category=s.category, metric_name=s.metric_name,
                               value=s.value, unit=s.unit, timestamp=to_utc_naive(s.timestamp)) for s in items)
        session.commit()
        return IngestResult(accepted=len(items))

    @app.post("/v1/op-stats", status_code=201)
    def ingest_op_stats(body: Annotated[OpStatsIn | list[OpStatsIn], Body()], session: SessionDep,
                        access: AccessDep) -> IngestResult:
        """Fast-path summaries: one row per operation per interval (latency histogram built in the SDK)."""
        items = body if isinstance(body, list) else [body]
        check_batch(session, access, items, "op-stats")
        session.add_all(OpStat(run_id=s.run_id, service=s.service, category=s.category, op_name=s.op_name,
                               attributes_json=json.dumps(s.attributes), interval_start=to_utc_naive(s.interval_start),
                               interval_end=to_utc_naive(s.interval_end), count=s.count, sum_ns=s.sum_ns,
                               min_ns=s.min_ns, max_ns=s.max_ns, p50_ns=s.p50_ns, p95_ns=s.p95_ns, p99_ns=s.p99_ns,
                               histogram_json=s.histogram.model_dump_json(), sdk_overhead_ns=s.sdk_overhead_ns)
                        for s in items)
        session.commit()
        return IngestResult(accepted=len(items))

    @app.get("/v1/runs/{run_id}/op-stats")
    def list_op_stats(run_id: str, session: SessionDep, access: AccessDep) -> list[dict]:
        load_run(session, access, run_id)
        rows = session.scalars(select(OpStat).where(OpStat.run_id == run_id).order_by(OpStat.interval_start, OpStat.id))
        return [{"op_name": r.op_name, "category": r.category, "attributes": json.loads(r.attributes_json),
                 "interval_start": iso_utc(r.interval_start), "interval_end": iso_utc(r.interval_end),
                 "count": r.count, "sum_ns": r.sum_ns, "min_ns": r.min_ns, "max_ns": r.max_ns,
                 "p50_ns": r.p50_ns, "p95_ns": r.p95_ns, "p99_ns": r.p99_ns,
                 "histogram": json.loads(r.histogram_json), "sdk_overhead_ns": r.sdk_overhead_ns} for r in rows]

    # ------------------------------------------------------------------ comparison / report

    def comparison_payload(session: Session, access: Access, baseline_run_id: str, remediated_run_id: str) -> dict:
        b, r = load_run(session, access, baseline_run_id), load_run(session, access, remediated_run_id)
        if b.run_id == r.run_id:
            raise HTTPException(422, "baseline and remediated run_id are the same run")
        if b.phase != "baseline":
            raise HTTPException(422, f"run {b.run_id!r} has phase {b.phase!r}; expected 'baseline'")
        if r.phase != "remediated":
            raise HTTPException(422, f"run {r.run_id!r} has phase {r.phase!r}; expected 'remediated'")
        if b.service != r.service:
            raise HTTPException(422, f"runs belong to different services ({b.service!r} vs {r.service!r})")

        def spans(run_id):
            return [SpanRec(s.span_id, s.parent_span_id, s.span_name, s.category, s.start_time, s.end_time, s.duration_ms,
                            json.loads(s.attributes_json)) for s in session.scalars(select(Span).where(Span.run_id == run_id))]

        def samples(run_id):
            return [SampleRec(s.metric_name, s.category, s.value, s.unit, s.timestamp)
                    for s in session.scalars(select(Sample).where(Sample.run_id == run_id))]

        def meta(run):
            d = run_out(session, run)
            return {k: d[k] for k in ("run_id", "service", "label", "phase", "created_at", "completed_at", "metadata")}

        return build_report(meta(b), meta(r), spans(b.run_id), spans(r.run_id), samples(b.run_id), samples(r.run_id))

    @app.get("/v1/comparison")
    def comparison(session: SessionDep, access: AccessDep, baseline_run_id: str, remediated_run_id: str) -> dict:
        """Single call: both flame-graph trees, span deltas, sampling deltas, run metadata, summary."""
        return comparison_payload(session, access, baseline_run_id, remediated_run_id)

    @app.get("/report", response_class=HTMLResponse)
    def report(session: SessionDep, access: AccessDep, baseline_run_id: str, remediated_run_id: str):
        try:
            return render_report(comparison_payload(session, access, baseline_run_id, remediated_run_id))
        except HTTPException as exc:
            return html_error(exc.status_code, str(exc.detail))

    @app.get("/", response_class=HTMLResponse)
    def index(session: SessionDep, access: AccessDep, page: int = Query(1, ge=1), page_size: int = Query(50, ge=1, le=500)):
        visible = scope_query(select(Run), access, Run)
        total = session.scalar(select(func.count()).select_from(visible.subquery()))
        runs = list(session.scalars(visible.order_by(Run.created_at.desc()).limit(page_size).offset((page - 1) * page_size)))
        return render_index(runs_out(session, runs), page=page, page_size=page_size, total=total)

    def html_error(status: int, cause: str) -> HTMLResponse:
        fixes = {404: "Check the run IDs, or pick both runs from the list of runs.",
                 422: "Pick one baseline run and one remediated run that belong to the same service."}
        return HTMLResponse(render_error(status, cause, fixes.get(status, "Go back and try again.")), status_code=status)

    HTML_ROUTES = {"/", "/report"}
    install_auth(app, auth, HTML_ROUTES)  # last: its middleware wraps every route above

    return app


app = create_app()
