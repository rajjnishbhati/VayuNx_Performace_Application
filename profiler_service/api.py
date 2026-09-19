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
from profiler_service.db import iso_utc, make_engine, make_sessionmaker, to_utc_naive
from profiler_service.models import Run, Sample, Span
from profiler_service.report_html import render_index, render_report
from profiler_service.schemas import IngestResult, RunComplete, RunCreate, RunOut, SampleIn, SpanIn


def get_session(request: Request):
    session: Session = request.app.state.sessionmaker()
    try:
        yield session
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def create_app(db_url: str | None = None) -> FastAPI:
    db_url = db_url or config.DB_URL

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.engine = make_engine(db_url)
        app.state.sessionmaker = make_sessionmaker(app.state.engine)
        yield
        app.state.engine.dispose()

    app = FastAPI(title="VAYUNX Profiler Service", version=__version__, lifespan=lifespan,
                  description="Language-agnostic span/sample ingestion, baseline-vs-remediated comparison and flame-graph reports. "
                              "Demo build - no auth.")

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
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
        spans = session.scalar(select(func.count()).select_from(Span).where(Span.run_id == run.run_id))
        samples = session.scalar(select(func.count()).select_from(Sample).where(Sample.run_id == run.run_id))
        return RunOut(run_id=run.run_id, service=run.service, label=run.label, phase=run.phase,
                      created_at=iso_utc(run.created_at), completed_at=iso_utc(run.completed_at),
                      metadata=json.loads(run.metadata_json), span_count=spans, sample_count=samples).model_dump()

    def load_run(session: Session, run_id: str) -> Run:
        run = session.get(Run, run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id!r} not found")
        return run

    def check_batch(session: Session, items: list, kind: str) -> None:
        if not items:
            raise HTTPException(422, f"empty {kind} batch")
        if len(items) > config.MAX_BATCH_ITEMS:
            raise HTTPException(413, f"batch of {len(items)} {kind}s exceeds limit of {config.MAX_BATCH_ITEMS}")
        runs: dict[str, Run] = {}
        for i, item in enumerate(items):
            run = runs.get(item.run_id) or session.get(Run, item.run_id)
            if run is None:
                raise HTTPException(404, {"error": f"run {item.run_id!r} not found", "index": i})
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
    def create_run(body: RunCreate, session: SessionDep) -> RunOut:
        run_id = body.run_id or uuid.uuid4().hex
        if session.get(Run, run_id) is not None:
            raise HTTPException(409, f"run {run_id!r} already exists")
        run = Run(run_id=run_id, service=body.service, label=body.label, phase=body.phase,
                  created_at=to_utc_naive(datetime.now(timezone.utc)), metadata_json=json.dumps(body.metadata))
        session.add(run)
        session.commit()
        return run_out(session, run)

    @app.get("/v1/runs")
    def list_runs(session: SessionDep, service: str | None = None, phase: str | None = None,
                  limit: int = Query(200, ge=1, le=1000)) -> list[RunOut]:
        q = select(Run).order_by(Run.created_at.desc()).limit(limit)
        if service:
            q = q.where(Run.service == service)
        if phase:
            q = q.where(Run.phase == phase)
        return [run_out(session, r) for r in session.scalars(q)]

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, session: SessionDep) -> RunOut:
        return run_out(session, load_run(session, run_id))

    @app.post("/v1/runs/{run_id}/complete")
    def complete_run(run_id: str, session: SessionDep, body: RunComplete | None = None) -> RunOut:
        run = load_run(session, run_id)
        if run.completed_at is not None:
            raise HTTPException(409, f"run {run_id!r} is already complete")
        completed = body.completed_at if body and body.completed_at else datetime.now(timezone.utc)
        run.completed_at = to_utc_naive(completed)
        session.commit()
        return run_out(session, run)

    # ------------------------------------------------------------------ ingestion (single object or array)

    @app.post("/v1/spans", status_code=201)
    def ingest_spans(body: Annotated[SpanIn | list[SpanIn], Body()], session: SessionDep) -> IngestResult:
        items = body if isinstance(body, list) else [body]
        check_batch(session, items, "span")
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
    def ingest_samples(body: Annotated[SampleIn | list[SampleIn], Body()], session: SessionDep) -> IngestResult:
        items = body if isinstance(body, list) else [body]
        check_batch(session, items, "sample")
        session.add_all(Sample(run_id=s.run_id, service=s.service, category=s.category, metric_name=s.metric_name,
                               value=s.value, unit=s.unit, timestamp=to_utc_naive(s.timestamp)) for s in items)
        session.commit()
        return IngestResult(accepted=len(items))

    # ------------------------------------------------------------------ comparison / report

    def comparison_payload(session: Session, baseline_run_id: str, remediated_run_id: str) -> dict:
        b, r = load_run(session, baseline_run_id), load_run(session, remediated_run_id)
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
    def comparison(session: SessionDep, baseline_run_id: str, remediated_run_id: str) -> dict:
        """Single call: both flame-graph trees, span deltas, sampling deltas, run metadata, summary."""
        return comparison_payload(session, baseline_run_id, remediated_run_id)

    @app.get("/report", response_class=HTMLResponse)
    def report(session: SessionDep, baseline_run_id: str, remediated_run_id: str):
        return render_report(comparison_payload(session, baseline_run_id, remediated_run_id))

    @app.get("/", response_class=HTMLResponse)
    def index(session: SessionDep):
        return render_index([run_out(session, r) for r in session.scalars(select(Run).order_by(Run.created_at.desc()).limit(500))])

    return app


app = create_app()
