"""Ships fast-path latency histograms as standard OTLP/HTTP metrics (POST /v1/metrics, protobuf).

Why not the OpenTelemetry metrics SDK for this: one Histogram.record() costs ~6.5 us in OTel Python 1.44
(measured on an i5-8400H), ~9x an MD5 call. Calls are aggregated into our own log2x8-ns histograms instead
(~0.4 us) and sent once per interval as an OTLP explicit-bucket histogram whose bounds match those buckets
exactly, so the receiver reconstructs them without loss.

Runs on its own daemon thread; failed exports stay pending (bounded, oldest dropped and counted) and are
retried on the next tick. Never raises into the host.
"""

from __future__ import annotations

import collections
import threading
import time
import urllib.error
import urllib.request

from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.common.v1.common_pb2 import AnyValue, KeyValue
from opentelemetry.proto.metrics.v1 import metrics_pb2

from vayunx_profiler_sdk.histogram import LatencyHistogram, bucket_bounds

METRIC = "vayunx.crypto.duration"


def _kv(key: str, value) -> KeyValue:
    if isinstance(value, bool):
        return KeyValue(key=key, value=AnyValue(bool_value=value))
    if isinstance(value, int):
        return KeyValue(key=key, value=AnyValue(int_value=value))
    if isinstance(value, float):
        return KeyValue(key=key, value=AnyValue(double_value=value))
    return KeyValue(key=key, value=AnyValue(string_value=str(value)))


def explicit_buckets(h: LatencyHistogram) -> tuple[list[float], list[int]]:
    """OTLP explicit bounds/counts equal to the log2x8 buckets: [lo, hi) in integer ns == (lo-1, hi-1]."""
    pairs: list[tuple[float, int]] = []
    for idx in sorted(h.buckets):
        lo, hi = bucket_bounds(idx)
        if not pairs or pairs[-1][0] < lo - 1:
            pairs.append((float(lo - 1), 0))
        pairs.append((float(hi - 1), h.buckets[idx]))
    return [b for b, _ in pairs], [c for _, c in pairs] + [0]


def build_request(resource_attrs: dict, start_ns: int, end_ns: int, hists: list) -> bytes:
    """`hists`: [(attribute pairs, LatencyHistogram), ...]"""
    req = ExportMetricsServiceRequest()
    rm = req.resource_metrics.add()
    rm.resource.attributes.extend(_kv(k, v) for k, v in resource_attrs.items() if v is not None)
    sm = rm.scope_metrics.add()
    sm.scope.name = "vayunx-python"
    metric = sm.metrics.add()
    metric.name, metric.unit = METRIC, "ns"
    metric.histogram.aggregation_temporality = metrics_pb2.AGGREGATION_TEMPORALITY_DELTA
    for attrs, h in hists:
        bounds, counts = explicit_buckets(h)
        dp = metric.histogram.data_points.add()
        dp.attributes.extend(_kv(k, v) for k, v in attrs if v is not None)
        dp.start_time_unix_nano, dp.time_unix_nano = start_ns, end_ns
        dp.count, dp.sum, dp.min, dp.max = h.count, float(h.sum_ns), float(h.min_ns), float(h.max_ns)
        dp.explicit_bounds.extend(bounds)
        dp.bucket_counts.extend(counts)
    return req.SerializeToString()


QUEUE_MAX = 200_000  # beyond this the calling thread aggregates inline (backpressure, nothing dropped)
AGGREGATE_EVERY_S = 0.1


class OpHistograms:
    """Per-series latency histograms (series = interned key id, see vayunx._hooks.KEY_ATTRS).

    Hot path: `queue.append((kid, ns))` - deque.append is atomic, so no lock (~50 ns instead of ~190 ns for
    the lock plus ~400 ns for a histogram update, measured on an i5-8400H). The shipper thread folds the
    queue into histograms every 100 ms; if it falls behind, callers fold inline once the queue is full."""

    def __init__(self):
        self.lock = threading.Lock()
        self.queue: collections.deque = collections.deque()
        self.hists: dict[int, LatencyHistogram] = {}
        self._start_ns = time.time_ns()

    def record(self, kid: int, ns: int) -> None:
        with self.lock:
            self._record(kid, ns)

    def _record(self, kid: int, ns: int) -> None:  # caller holds lock
        h = self.hists.get(kid)
        if h is None:
            h = self.hists[kid] = LatencyHistogram()
        h.record(ns)

    def aggregate(self) -> int:
        """Fold queued samples into the histograms. Returns how many were folded."""
        popleft, n = self.queue.popleft, 0
        with self.lock:
            while True:
                try:
                    kid, ns = popleft()
                except IndexError:
                    return n
                self._record(kid, ns)
                n += 1

    def drain(self) -> tuple[int, int, dict]:
        self.aggregate()
        with self.lock:
            drained, self.hists = self.hists, {}
            start, self._start_ns = self._start_ns, time.time_ns()
        return start, self._start_ns, drained


class Shipper(threading.Thread):
    def __init__(self, cfg, resource_attrs: dict, ophists: OpHistograms, counters: dict):
        super().__init__(name="vayunx-histogram-shipper", daemon=True)
        self.cfg, self.resource_attrs, self.ophists, self.counters = cfg, resource_attrs, ophists, counters
        self.url = f"{cfg.endpoint}/v1/metrics"
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._pending: list[bytes] = []

    def run(self) -> None:
        next_ship = time.monotonic() + self.cfg.export_interval_s
        while not self._stop.wait(min(AGGREGATE_EVERY_S, self.cfg.export_interval_s)):
            try:
                self.ophists.aggregate()
            except Exception:
                self.counters["internal_errors"] = self.counters.get("internal_errors", 0) + 1
            if time.monotonic() >= next_ship:
                self._tick(time.monotonic() + self.cfg.flush_timeout_s)
                next_ship = time.monotonic() + self.cfg.export_interval_s

    def _tick(self, deadline: float) -> bool:
        try:
            start, end, hists = self.ophists.drain()
            with self._lock:
                if hists:
                    from vayunx._hooks import KEY_ATTRS
                    series = [(KEY_ATTRS[kid], h) for kid, h in hists.items()]
                    self._pending.append(build_request(self.resource_attrs, start, end, series))
                    while len(self._pending) > self.cfg.max_pending_exports:
                        self._pending.pop(0)
                        self.counters["dropped_exports"] = self.counters.get("dropped_exports", 0) + 1
                while self._pending and time.monotonic() < deadline:
                    if not self._post(self._pending[0], max(0.1, deadline - time.monotonic())):
                        return False
                    self._pending.pop(0)
                    self.counters["exports_sent"] = self.counters.get("exports_sent", 0) + 1
                return not self._pending
        except Exception:  # never let the shipper die or raise
            self.counters["internal_errors"] = self.counters.get("internal_errors", 0) + 1
            return False

    def _post(self, body: bytes, timeout: float) -> bool:
        req = urllib.request.Request(self.url, data=body, method="POST", headers={"Content-Type": "application/x-protobuf", **self.cfg.auth_headers()})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return 200 <= r.status < 300
        except urllib.error.HTTPError as exc:
            if 400 <= exc.code < 500 and exc.code not in (408, 429):  # permanent: drop it rather than retry forever
                self.counters["rejected_exports"] = self.counters.get("rejected_exports", 0) + 1
                return True
            return False
        except (urllib.error.URLError, OSError, TimeoutError):
            self.counters["offline_attempts"] = self.counters.get("offline_attempts", 0) + 1
            return False

    def stop(self, timeout_s: float) -> bool:
        self._stop.set()
        return self._tick(time.monotonic() + timeout_s)
