"""OTLP/HTTP receiver: POST /v1/traces and POST /v1/metrics (protobuf or OTLP-JSON, optionally gzip).

Lets any OpenTelemetry SDK (Python, Node, Next.js...) report to the Profiler Service. Mapping:
* Resource attributes -> Run. `service.name` is the service; `vayunx.variant` the variant (e.g. "md5");
  `vayunx.run_id` the run (set by the VAYUNX SDKs per process run; if absent a deterministic id is derived
  from service, variant and service.instance.id). Runs from OTLP have source "app" and stay open.
* Spans -> spans. Any `crypto.*` attribute makes the span category "cryptographic". Attributes are privacy
  scrubbed (vayunx_profiler_sdk.privacy) and capped. Duplicate span ids are skipped (exporter retries).
* Metric `vayunx.crypto.duration` (exponential or explicit-bucket histogram) -> op_stats, converted to the
  log2x8-ns histogram. DELTA points are added; for CUMULATIVE points the latest point replaces the series.
* Gauges named in GAUGES -> samples.
Unsupported data is ignored, not rejected; rejected spans are reported via OTLP partial success.
"""

import base64
import gzip
import hashlib
import json
import math
import re
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from google.protobuf import json_format
from google.protobuf.message import DecodeError
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import (
    ExportMetricsServiceRequest, ExportMetricsServiceResponse,
)
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest, ExportTraceServiceResponse
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from profiler_service.access import access_for
from profiler_service.config import MAX_ATTRIBUTES
from profiler_service.db import to_utc_naive
from profiler_service.models import OpStat, Run, Sample, Span
from vayunx_profiler_sdk.histogram import LatencyHistogram, bucket_bounds, bucket_index
from vayunx_profiler_sdk.privacy import clean_attributes

router = APIRouter(tags=["otlp"])
MAX_BODY = 16 * 1024 * 1024
ID_RE = re.compile(r"^[A-Za-z0-9._:\-]{1,64}$")
CRYPTO_METRIC = "vayunx.crypto.duration"
UNIT_TO_NS = {"ns": 1.0, "us": 1e3, "µs": 1e3, "ms": 1e6, "s": 1e9}
GAUGES = {  # OTLP metric name -> (our metric name, unit)
    "vayunx.process.cores_busy": ("proc_cores_busy", "cores"),
    "vayunx.process.cpu_time": ("proc_cpu_time_s", "s"),
    "vayunx.process.rss_mib": ("proc_rss_mib", "MiB"),
    "vayunx.process.peak_rss_mib": ("proc_peak_rss_mib", "MiB"),
    "vayunx.process.threads": ("proc_threads", "count"),
    "vayunx.machine.cpu_pct": ("machine_cpu_pct", "%"),
    "vayunx.machine.other_cores_busy": ("machine_other_cores_busy", "cores"),
    "vayunx.machine.mem_available_mib": ("machine_mem_available_mib", "MiB"),
    "vayunx.node.eventloop.delay_p99_ms": ("node_eventloop_delay_p99_ms", "ms"),
    "vayunx.node.eventloop.utilization": ("node_eventloop_utilization", "ratio"),
    "vayunx.node.threadpool.wait_ms": ("node_threadpool_wait_ms", "ms"),
}
_HEX_ID_KEYS = {"traceId", "spanId", "parentSpanId"}


# ----------------------------------------------------------------------------- decoding


class BadRequest(ValueError):
    pass


def _hex_ids_to_base64(obj):
    """OTLP-JSON encodes trace/span ids as hex; protobuf JSON mapping expects base64 for bytes fields."""
    if isinstance(obj, dict):
        return {k: (base64.b64encode(bytes.fromhex(v)).decode() if k in _HEX_ID_KEYS and isinstance(v, str) and v
                    else _hex_ids_to_base64(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_hex_ids_to_base64(x) for x in obj]
    return obj


async def _decode(request: Request, message_cls):
    raw = await request.body()
    if len(raw) > MAX_BODY:
        raise BadRequest(f"body larger than {MAX_BODY} bytes")
    if request.headers.get("content-encoding", "").lower() == "gzip":
        try:
            raw = gzip.decompress(raw)
        except OSError as exc:
            raise BadRequest(f"invalid gzip body: {exc}") from None
    ctype = request.headers.get("content-type", "application/x-protobuf").split(";")[0].strip().lower()
    msg = message_cls()
    try:
        if ctype == "application/json":
            json_format.ParseDict(_hex_ids_to_base64(json.loads(raw)), msg, ignore_unknown_fields=True)
        else:
            msg.ParseFromString(raw)
    except (DecodeError, json_format.ParseError, ValueError) as exc:
        raise BadRequest(f"could not decode OTLP {ctype} body: {exc}") from None
    return msg, ctype == "application/json"


def _respond(response_msg, as_json: bool):
    if as_json:
        return JSONResponse(json_format.MessageToDict(response_msg))
    return Response(response_msg.SerializeToString(), media_type="application/x-protobuf")


def _bad(exc: BadRequest):
    return JSONResponse({"detail": {"error": str(exc), "fix": "Send an OTLP/HTTP ExportTraceServiceRequest or "
                                                                    "ExportMetricsServiceRequest (protobuf or JSON)."}},
                        status_code=400)


# ----------------------------------------------------------------------------- mapping helpers


def _value(any_value):
    kind = any_value.WhichOneof("value")
    if kind in ("string_value", "bool_value", "int_value", "double_value"):
        return getattr(any_value, kind)
    return None  # arrays, kvlists and bytes are not kept


def _attrs(kvs) -> dict:
    out = {}
    for kv in kvs:
        v = _value(kv.value)
        if v is not None:
            out[kv.key] = v
    return out


def _dt(ns: int) -> datetime:
    return to_utc_naive(datetime.fromtimestamp(ns / 1e9, tz=timezone.utc))


def _clean(attrs: dict) -> dict:
    clean, _ = clean_attributes(attrs)
    return dict(list(clean.items())[:MAX_ATTRIBUTES])


def _project(access, res: dict, session: Session) -> str:
    """The project for a new run: resource attribute vayunx.project, else the token's, else Default."""
    requested = res.get("vayunx.project")
    try:
        return access.write_project(str(requested)[:64] if requested else None, session)
    except HTTPException as exc:
        raise BadRequest(exc.detail["error"] if isinstance(exc.detail, dict) else str(exc.detail)) from None


def _resolve_run(session: Session, res: dict, access) -> Run:
    service = str(res.get("service.name") or "unknown_service")[:128]
    variant = res.get("vayunx.variant")
    variant = str(variant)[:256] if variant else None
    rid = res.get("vayunx.run_id")
    if not (isinstance(rid, str) and ID_RE.match(rid)):
        seed = f"{service}|{variant}|{res.get('service.instance.id', '')}"
        rid = hashlib.sha1(seed.encode()).hexdigest()[:32]
    run = session.get(Run, rid)
    if run is not None:
        if not access.can(run.project_id, "editor"):
            raise BadRequest(f"run {rid!r} is not writable with these credentials")
        if run.service != service:
            raise BadRequest(f"run {rid!r} belongs to service {run.service!r}, not {service!r}")
        return run
    phase = res.get("vayunx.phase") if res.get("vayunx.phase") in ("baseline", "remediated") else "baseline"
    meta = _clean({k: v for k, v in res.items() if not k.startswith("vayunx.run_id")})
    run = Run(run_id=rid, service=service, label=variant or service, phase=phase,
              created_at=to_utc_naive(datetime.now(timezone.utc)), metadata_json=json.dumps(meta),
              variant=variant, source="app", project_id=_project(access, res, session))
    session.add(run)
    session.flush()
    return run


# ----------------------------------------------------------------------------- traces


@router.post("/v1/traces")
async def otlp_traces(request: Request):
    try:
        req, as_json = await _decode(request, ExportTraceServiceRequest)
    except BadRequest as exc:
        return _bad(exc)
    rejected, errors = 0, []
    with request.app.state.sessionmaker() as session:
        access = access_for(request, session)
        for rs in req.resource_spans:
            res = _attrs(rs.resource.attributes)
            spans = [sp for ss in rs.scope_spans for sp in ss.spans]
            try:
                run = _resolve_run(session, res, access)
            except BadRequest as exc:
                rejected += len(spans)
                errors.append(str(exc))
                continue
            ids = [sp.span_id.hex() for sp in spans]
            existing = set(session.scalars(select(Span.span_id).where(Span.run_id == run.run_id, Span.span_id.in_(ids))))
            for sp in spans:
                sid = sp.span_id.hex()
                if not sid or sid in existing:
                    continue
                existing.add(sid)
                attrs = _clean(_attrs(sp.attributes))
                if sp.status.code == 2:  # STATUS_CODE_ERROR
                    attrs["error"] = sp.status.message or "ERROR"
                crypto = any(k.startswith("crypto.") for k in attrs)
                start, end = sp.start_time_unix_nano, max(sp.end_time_unix_nano, sp.start_time_unix_nano)
                session.add(Span(run_id=run.run_id, span_id=sid, parent_span_id=sp.parent_span_id.hex() or None,
                                 service=run.service, category="cryptographic" if crypto else "general",
                                 span_name=sp.name[:256] or "unnamed", start_time=_dt(start), end_time=_dt(end),
                                 duration_ms=(end - start) / 1e6, attributes_json=json.dumps(attrs)))
        session.commit()
    resp = ExportTraceServiceResponse()
    if rejected:
        resp.partial_success.rejected_spans = rejected
        resp.partial_success.error_message = "; ".join(errors)[:1000]
    return _respond(resp, as_json)


# ----------------------------------------------------------------------------- metrics


def _exp_to_hist(dp, scale_ns: float) -> LatencyHistogram:
    h = LatencyHistogram()
    base = 2.0 ** (2.0 ** -dp.scale)
    for i, c in enumerate(dp.positive.bucket_counts):
        if not c:
            continue
        k = dp.positive.offset + i
        mid = math.sqrt(base ** k * base ** (k + 1)) * scale_ns  # geometric midpoint of (base^k, base^(k+1)]
        idx = bucket_index(int(round(mid)))
        h.buckets[idx] = h.buckets.get(idx, 0) + c
    if dp.zero_count:
        h.buckets[0] = h.buckets.get(0, 0) + dp.zero_count
    h.count = sum(h.buckets.values())
    return h


def _explicit_to_hist(dp, scale_ns: float) -> LatencyHistogram:
    h = LatencyHistogram()
    bounds = list(dp.explicit_bounds)
    for i, c in enumerate(dp.bucket_counts):
        if not c:
            continue
        lo = bounds[i - 1] if i > 0 else (dp.min if dp.HasField("min") else 0.0)
        hi = bounds[i] if i < len(bounds) else (dp.max if dp.HasField("max") else lo)
        idx = bucket_index(int(round((lo + hi) / 2 * scale_ns)))
        h.buckets[idx] = h.buckets.get(idx, 0) + c
    h.count = sum(h.buckets.values())
    return h


def _store_histogram(session: Session, run: Run, dp, hist: LatencyHistogram, scale_ns: float, cumulative: bool) -> None:
    if not hist.count:
        return
    attrs = _clean({k: v for k, v in _attrs(dp.attributes).items()})
    hist.sum_ns = int(round(dp.sum * scale_ns)) if dp.HasField("sum") else 0
    lo_idx, hi_idx = min(hist.buckets), max(hist.buckets)
    hist.min_ns = int(round(dp.min * scale_ns)) if dp.HasField("min") else None
    hist.max_ns = int(round(dp.max * scale_ns)) if dp.HasField("max") else None
    if hist.min_ns is None or hist.max_ns is None:  # derive from bucket bounds when the SDK did not send min/max
        hist.min_ns = hist.min_ns if hist.min_ns is not None else bucket_bounds(lo_idx)[0]
        hist.max_ns = hist.max_ns if hist.max_ns is not None else bucket_bounds(hi_idx)[1] - 1
    op_name = str(attrs.get("crypto.operation") or "crypto")[:256]
    attrs_json = json.dumps(attrs, sort_keys=True)
    if cumulative:  # the newest cumulative point supersedes earlier points of the same series
        session.execute(delete(OpStat).where(OpStat.run_id == run.run_id, OpStat.op_name == op_name,
                                             OpStat.attributes_json == attrs_json))
    start = dp.start_time_unix_nano or dp.time_unix_nano
    session.add(OpStat(run_id=run.run_id, service=run.service, category="cryptographic", op_name=op_name,
                       attributes_json=attrs_json, interval_start=_dt(start), interval_end=_dt(dp.time_unix_nano),
                       count=hist.count, sum_ns=hist.sum_ns, min_ns=hist.min_ns, max_ns=hist.max_ns,
                       p50_ns=hist.percentile(50), p95_ns=hist.percentile(95), p99_ns=hist.percentile(99),
                       histogram_json=json.dumps(hist.to_dict()), sdk_overhead_ns=None))


@router.post("/v1/metrics")
async def otlp_metrics(request: Request):
    try:
        req, as_json = await _decode(request, ExportMetricsServiceRequest)
    except BadRequest as exc:
        return _bad(exc)
    rejected, errors = 0, []
    with request.app.state.sessionmaker() as session:
        access = access_for(request, session)
        for rm in req.resource_metrics:
            res = _attrs(rm.resource.attributes)
            try:
                run = _resolve_run(session, res, access)
            except BadRequest as exc:
                rejected += sum(1 for sm in rm.scope_metrics for _ in sm.metrics)
                errors.append(str(exc))
                continue
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    kind = m.WhichOneof("data")
                    if m.name == CRYPTO_METRIC and kind in ("exponential_histogram", "histogram"):
                        scale_ns = UNIT_TO_NS.get(m.unit, 1.0)
                        data = getattr(m, kind)
                        cumulative = data.aggregation_temporality == 2  # AGGREGATION_TEMPORALITY_CUMULATIVE
                        for dp in data.data_points:
                            hist = _exp_to_hist(dp, scale_ns) if kind == "exponential_histogram" else _explicit_to_hist(dp, scale_ns)
                            _store_histogram(session, run, dp, hist, scale_ns, cumulative)
                    elif m.name in GAUGES and kind in ("gauge", "sum"):
                        name, unit = GAUGES[m.name]
                        for dp in getattr(m, kind).data_points:
                            value = dp.as_double if dp.WhichOneof("value") == "as_double" else float(dp.as_int)
                            attrs = _attrs(dp.attributes)
                            cat = attrs.get("vayunx.category") if attrs.get("vayunx.category") in ("general", "cryptographic") else "general"
                            session.add(Sample(run_id=run.run_id, service=run.service, category=cat, metric_name=name,
                                               value=value, unit=unit, timestamp=_dt(dp.time_unix_nano)))
        session.commit()
    resp = ExportMetricsServiceResponse()
    if rejected:
        resp.partial_success.rejected_data_points = rejected
        resp.partial_success.error_message = "; ".join(errors)[:1000]
    return _respond(resp, as_json)
