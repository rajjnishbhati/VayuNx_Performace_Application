"""OTLP/HTTP receiver (/v1/traces, /v1/metrics): real OpenTelemetry SDK exporters, OTLP-JSON (Node) and gzip."""

import gzip
import json
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest
import uvicorn
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import Observation
from opentelemetry.sdk.metrics import Histogram, MeterProvider
from opentelemetry.sdk.metrics.export import AggregationTemporality, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExponentialBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor

from profiler_service.api import create_app


@pytest.fixture(scope="module")
def live(tmp_path_factory, db_url):
    app = create_app(db_url(tmp_path_factory.mktemp("db")))
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    thread.join(timeout=10)


def get(url):
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def post(url, body: bytes, content_type: str, encoding: str | None = None):
    headers = {"Content-Type": content_type}
    if encoding:
        headers["Content-Encoding"] = encoding
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def resource(service, variant, run_id=None):
    attrs = {"service.name": service, "vayunx.variant": variant, "telemetry.sdk.language": "python"}
    if run_id:
        attrs["vayunx.run_id"] = run_id
    return Resource.create(attrs)


def run_of(live, service):
    runs = get(f"{live}/v2/runs?service={service}")["items"]
    assert len(runs) == 1, runs
    return runs[0]


def test_real_sdk_spans_become_a_run_with_a_span_tree(live):
    rid = uuid.uuid4().hex
    provider = TracerProvider(resource=resource("otel-traces", "md5", rid))
    provider.add_span_processor(SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"{live}/v1/traces")))
    tracer = provider.get_tracer("test")
    with tracer.start_as_current_span("login"):
        with tracer.start_as_current_span("hash_password", attributes={"crypto.algorithm": "MD5", "crypto.operation": "hash",
                                                                       "crypto.input_bytes": 28, "password": "hunter2"}):
            pass
    provider.shutdown()

    run = run_of(live, "otel-traces")
    assert run["run_id"] == rid and run["variant"] == "md5" and run["source"] == "app" and run["span_count"] == 2
    assert run["metadata"]["telemetry.sdk.language"] == "python"
    spans = {s["span_name"]: s for s in get(f"{live}/v2/runs/{rid}/spans")}
    assert spans["hash_password"]["parent_span_id"] == spans["login"]["span_id"]
    assert "password" not in spans["hash_password"]["attributes"]


def test_span_details_parent_category_and_privacy(live):
    body = json.dumps({"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": "json-app"}},
                                    {"key": "vayunx.variant", "value": {"stringValue": "argon2id"}}]},
        "scopeSpans": [{"scope": {"name": "node"}, "spans": [
            {"traceId": "5b8efff798038103d269b633813fc60c", "spanId": "aaaaaaaaaaaaaaa1", "name": "login",
             "startTimeUnixNano": "1760000000000000000", "endTimeUnixNano": "1760000000060000000", "attributes": []},
            {"traceId": "5b8efff798038103d269b633813fc60c", "spanId": "aaaaaaaaaaaaaaa2", "parentSpanId": "aaaaaaaaaaaaaaa1",
             "name": "argon2.hash", "startTimeUnixNano": "1760000000005000000", "endTimeUnixNano": "1760000000055000000",
             "attributes": [{"key": "crypto.algorithm", "value": {"stringValue": "Argon2id"}},
                            {"key": "crypto.input_bytes", "value": {"intValue": "28"}},
                            {"key": "crypto.sync", "value": {"boolValue": True}},
                            {"key": "password", "value": {"stringValue": "hunter2"}}],
             "status": {"code": 2, "message": "boom"}},
        ]}]}]}).encode()
    status, _ = post(f"{live}/v1/traces", body, "application/json")
    assert status == 200
    status, _ = post(f"{live}/v1/traces", body, "application/json")  # an exporter retry must be idempotent
    assert status == 200
    run = run_of(live, "json-app")
    assert run["span_count"] == 2 and run["variant"] == "argon2id"
    by_name = {s["span_name"]: s for s in get(f"{live}/v2/runs/{run['run_id']}/spans")}
    child = by_name["argon2.hash"]
    assert child["parent_span_id"] == "aaaaaaaaaaaaaaa1" and child["category"] == "cryptographic"
    assert child["duration_ms"] == pytest.approx(50.0)
    assert child["attributes"] == {"crypto.algorithm": "Argon2id", "crypto.input_bytes": 28, "crypto.sync": True, "error": "boom"}
    assert by_name["login"]["category"] == "general"


def test_gzip_protobuf_is_accepted(live):
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
    from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
    req = ExportTraceServiceRequest()
    rs = req.resource_spans.add()
    rs.resource.attributes.append(KeyValue(key="service.name", value=AnyValue(string_value="gzip-app")))
    span = rs.scope_spans.add().spans.add()
    span.trace_id, span.span_id, span.name = bytes(16), bytes.fromhex("00000000000000ab"), "sha256"
    span.start_time_unix_nano, span.end_time_unix_nano = 1_760_000_000_000_000_000, 1_760_000_000_000_010_000
    span.attributes.append(KeyValue(key="crypto.algorithm", value=AnyValue(string_value="SHA-256")))
    status, _ = post(f"{live}/v1/traces", gzip.compress(req.SerializeToString()), "application/x-protobuf", "gzip")
    assert status == 200 and run_of(live, "gzip-app")["span_count"] == 1


def test_malformed_bodies_get_a_clean_400(live):
    assert post(f"{live}/v1/traces", b"not protobuf \xff", "application/x-protobuf")[0] == 400
    assert post(f"{live}/v1/traces", b"{not json", "application/json")[0] == 400


def _metrics_provider(live, service, variant, rid, temporality):
    exporter = OTLPMetricExporter(endpoint=f"{live}/v1/metrics", preferred_temporality={Histogram: temporality})
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=3_600_000)
    view = View(instrument_name="vayunx.crypto.duration", aggregation=ExponentialBucketHistogramAggregation(max_scale=3))
    return MeterProvider(resource=resource(service, variant, rid), metric_readers=[reader], views=[view])


def test_exponential_histogram_becomes_op_stats(live):
    rid = uuid.uuid4().hex
    mp = _metrics_provider(live, "otel-metrics", "argon2id", rid, AggregationTemporality.DELTA)
    meter = mp.get_meter("test")
    hist = meter.create_histogram("vayunx.crypto.duration", unit="ns")
    values = [int(38e6 * (0.95 + 0.1 * i / 999)) for i in range(1000)]
    for v in values:
        hist.record(v, {"crypto.operation": "hash", "crypto.algorithm": "Argon2id"})
    meter.create_observable_gauge("vayunx.process.cores_busy",
                                  callbacks=[lambda options: [Observation(3.3, {"vayunx.category": "cryptographic"})]])
    mp.force_flush()
    for v in values[:10]:
        hist.record(v, {"crypto.operation": "hash", "crypto.algorithm": "Argon2id"})
    mp.shutdown()  # final delta export

    stats = get(f"{live}/v1/runs/{rid}/op-stats")
    assert sum(s["count"] for s in stats) == 1010  # two delta exports, summed, not double counted
    s = stats[0]
    assert s["op_name"] == "hash" and s["attributes"]["crypto.algorithm"] == "Argon2id"
    assert s["min_ns"] == min(values) and s["max_ns"] == max(values)
    assert s["p50_ns"] == pytest.approx(38e6, rel=0.10)
    samples = get(f"{live}/v2/runs/{rid}/samples")
    assert any(x["metric_name"] == "proc_cores_busy" and x["value"] == 3.3 and x["category"] == "cryptographic" for x in samples)


def test_cumulative_temporality_is_not_double_counted(live):
    rid = uuid.uuid4().hex
    mp = _metrics_provider(live, "otel-cumulative", "md5", rid, AggregationTemporality.CUMULATIVE)
    hist = mp.get_meter("test").create_histogram("vayunx.crypto.duration", unit="ns")
    for _ in range(100):
        hist.record(1000, {"crypto.operation": "hash"})
    mp.force_flush()
    for _ in range(50):
        hist.record(1000, {"crypto.operation": "hash"})
    mp.shutdown()
    stats = get(f"{live}/v1/runs/{rid}/op-stats")
    assert sum(s["count"] for s in stats) == 150  # latest cumulative point replaces the previous one


def test_v2_compare_works_on_otel_runs(live):
    run_ids = []
    for variant, center in (("md5", 1_100), ("argon2id", 55_000_000)):
        rid = uuid.uuid4().hex
        mp = _metrics_provider(live, "otel-compare", variant, rid, AggregationTemporality.DELTA)
        hist = mp.get_meter("test").create_histogram("vayunx.crypto.duration", unit="ns")
        for i in range(200):
            hist.record(int(center * (0.97 + 0.06 * i / 199)), {"crypto.operation": "hash", "crypto.algorithm": variant})
        mp.shutdown()
        run_ids.append(rid)
    cmp = get(f"{live}/v2/compare?run_ids={','.join(run_ids)}")
    assert cmp["operation"] == "hash" and [v["key"] for v in cmp["variants"]] == ["md5", "argon2id"]
    assert cmp["variants"][1]["vs_reference"]["time_ratio"] > 1000
    assert cmp["variants"][1]["security"]["safe_for_passwords"] is True
