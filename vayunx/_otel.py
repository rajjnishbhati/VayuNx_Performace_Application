"""OpenTelemetry providers: traces (spans) and process/machine gauges, exported over OTLP/HTTP."""

from __future__ import annotations

import logging
import platform
import socket
import threading
import time

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.metrics import CallbackOptions, Observation
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from vayunx._cpuclock import thread_cpu_ns

CPU_ATTR = "vayunx.cpu_time_ms"


class _OncePerMinute(logging.Filter):
    """The OTLP exporters log an error on every failed export - once a second while the service is down.
    Offline mode must be quiet (spec A): let the first message through, then at most one a minute."""

    def __init__(self):
        super().__init__()
        self.next_at = 0.0
        self.suppressed = 0

    def filter(self, record: logging.LogRecord) -> bool:
        now = time.monotonic()
        if record.levelno >= logging.WARNING and now < self.next_at:
            self.suppressed += 1
            return False
        if record.levelno >= logging.WARNING:
            self.next_at = now + 60.0
            if self.suppressed:
                record.msg = f"{record.msg} ({self.suppressed} similar messages suppressed by vayunx)"
                self.suppressed = 0
        return True


EXPORT_LOG_FILTER = _OncePerMinute()
for _name in ("opentelemetry.exporter.otlp.proto.http.trace_exporter",
              "opentelemetry.exporter.otlp.proto.http.metric_exporter"):
    logging.getLogger(_name).addFilter(EXPORT_LOG_FILTER)


def resource_attrs(cfg, version: str) -> dict:
    attrs = {
        "service.name": cfg.service, "vayunx.run_id": cfg.run_id, "vayunx.variant": cfg.variant, "vayunx.phase": cfg.phase,
        "vayunx.sdk": f"vayunx-python/{version}", "telemetry.sdk.language": "python",
        "process.runtime.name": platform.python_implementation(), "process.runtime.version": platform.python_version(),
        "host.name": socket.gethostname(), "os.type": platform.system().lower(),
    }
    return {k: v for k, v in attrs.items() if v is not None}


class CpuTimeSpanProcessor(SpanProcessor):
    """Adds `vayunx.cpu_time_ms` (thread CPU time) to spans that start and end on the same thread.
    Spans that already carry the attribute (the SDK's retroactive crypto spans) are left alone."""

    def on_start(self, span, parent_context=None) -> None:
        try:
            if span.attributes and CPU_ATTR in span.attributes:
                return
            tid, c0 = threading.get_ident(), thread_cpu_ns()
            original_end = span.end

            def end(end_time=None):
                try:
                    if threading.get_ident() == tid:
                        span.set_attribute(CPU_ATTR, (thread_cpu_ns() - c0) / 1e6)
                except Exception:
                    pass
                original_end(end_time)

            span.end = end
        except Exception:
            pass

    def on_end(self, span) -> None:
        pass

    def shutdown(self) -> None:
        pass

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return True


def tracer_provider(cfg, attrs: dict) -> TracerProvider:
    tp = TracerProvider(resource=Resource.create(attrs), shutdown_on_exit=False)
    tp.add_span_processor(CpuTimeSpanProcessor())
    exporter = OTLPSpanExporter(endpoint=f"{cfg.endpoint}/v1/traces", timeout=cfg.flush_timeout_s, headers=cfg.auth_headers())
    tp.add_span_processor(BatchSpanProcessor(exporter, max_queue_size=2048, max_export_batch_size=512,
                                             schedule_delay_millis=1000, export_timeout_millis=int(cfg.flush_timeout_s * 1000)))
    return tp


class _GaugeReader:
    """Reads psutil once per collection and serves all gauges from that snapshot."""

    def __init__(self, activity):
        from vayunx_profiler_sdk.metrics import MachineReader, ProcessReader
        self.activity = activity
        self.proc, self.machine = ProcessReader(), MachineReader()
        self.proc.prime()
        self.machine.prime()
        self._snap: dict = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        with self._lock:
            now = time.monotonic()
            if now - self._at > 0.25:
                p, m = self.proc.read(full=True), self.machine.read(full=False)
                self._snap = {**p, **m, "machine_other_cores_busy": self.machine.other_cores_busy(m, p)}
                self._at = now
            return self._snap

    def gauge(self, key: str):
        def callback(options: CallbackOptions):
            try:
                value = self.snapshot().get(key)
                if value is None:
                    return []
                category = "cryptographic" if self.activity.active() else "general"
                return [Observation(float(value), {"vayunx.category": category})]
            except Exception:
                return []
        return callback


GAUGES = {  # OTLP name -> key in the snapshot (see profiler_service/otlp.py GAUGES for the receiving side)
    "vayunx.process.cores_busy": "proc_cores_busy", "vayunx.process.cpu_time": "proc_cpu_time_s",
    "vayunx.process.rss_mib": "proc_rss_mib", "vayunx.process.peak_rss_mib": "proc_peak_rss_mib",
    "vayunx.process.threads": "proc_threads", "vayunx.machine.cpu_pct": "machine_cpu_pct",
    "vayunx.machine.other_cores_busy": "machine_other_cores_busy", "vayunx.machine.mem_available_mib": "machine_mem_available_mib",
}


def meter_provider(cfg, attrs: dict, activity) -> MeterProvider:
    exporter = OTLPMetricExporter(endpoint=f"{cfg.endpoint}/v1/metrics", timeout=cfg.flush_timeout_s, headers=cfg.auth_headers())
    reader = PeriodicExportingMetricReader(exporter, export_interval_millis=int(cfg.gauge_interval_s * 1000),
                                           export_timeout_millis=int(cfg.flush_timeout_s * 1000))
    mp = MeterProvider(resource=Resource.create(attrs), metric_readers=[reader], shutdown_on_exit=False)
    reader_obj = _GaugeReader(activity)
    meter = mp.get_meter("vayunx-python")
    for name, key in GAUGES.items():
        meter.create_observable_gauge(name, callbacks=[reader_obj.gauge(key)])
    return mp
