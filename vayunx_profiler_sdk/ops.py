"""Fast-path operation timers.

A span costs several microseconds, which is more than a fast primitive such as MD5 (~1 us).
An Op instead records each call into an in-process latency histogram and sends one summary per
interval (POST /v1/op-stats). Full spans are kept only for slow calls (>= slow_ms) or for every
Nth call (span_sample_every).

    md5 = run.op("md5_hash", attributes={"crypto.algorithm": "MD5"})
    with md5:
        hashlib.md5(data).digest()

An Op is safe to share between threads. It is not re-entrant within one thread (don't nest the
same Op inside itself).
"""

from __future__ import annotations

import functools
import statistics
import threading
import time
from datetime import datetime, timedelta, timezone

from vayunx_profiler_sdk.histogram import LatencyHistogram, bucket_index

_perf = time.perf_counter_ns


class Op:
    def __init__(self, name: str, category: str, attributes: dict, slow_ms: float | None, span_sample_every: int,
                 on_span=None, on_error=None):
        self.name, self.category, self.attributes = name, category, attributes
        self._slow_ns = None if slow_ms is None else int(slow_ms * 1_000_000)
        self._every = max(0, int(span_sample_every or 0))
        self._on_span, self._on_error = on_span, on_error
        self._tls = threading.local()
        self._lock = threading.Lock()
        self._hist = LatencyHistogram()
        self._calls = 0
        self._interval_start = datetime.now(timezone.utc)

    def __enter__(self):
        self._tls.t0 = _perf()
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = _perf() - self._tls.t0
        try:
            with self._lock:
                h = self._hist
                h.count += 1
                h.sum_ns += elapsed
                if h.min_ns is None or elapsed < h.min_ns:
                    h.min_ns = elapsed
                if h.max_ns is None or elapsed > h.max_ns:
                    h.max_ns = elapsed
                idx = bucket_index(elapsed)
                h.buckets[idx] = h.buckets.get(idx, 0) + 1
                self._calls += 1
                n = self._calls
            if self._on_span is not None:
                if self._slow_ns is not None and elapsed >= self._slow_ns:
                    self._on_span(self, elapsed, "slow", exc_type)
                elif self._every and n % self._every == 0:
                    self._on_span(self, elapsed, "sampled", exc_type)
        except Exception as err:  # never raise into host code
            if self._on_error:
                self._on_error("op", err)
        return False

    def wrap(self, fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with self:
                return fn(*args, **kwargs)
        return wrapper

    def drain(self) -> tuple[LatencyHistogram, datetime, datetime] | None:
        """Swap out the current interval's histogram. None if no calls since the last drain."""
        now = datetime.now(timezone.utc)
        with self._lock:
            if not self._hist.count:
                return None
            hist, start = self._hist, self._interval_start
            self._hist, self._interval_start = LatencyHistogram(), now
        return hist, start, now


def span_times(elapsed_ns: int) -> tuple[datetime, datetime]:
    """UTC start/end for a span that just ended, anchored on the wall clock at exit (serialised by the sender)."""
    end = datetime.now(timezone.utc)
    return end - timedelta(microseconds=elapsed_ns / 1000), end


def measure_op_overhead_ns(iterations: int = 5000, repeats: int = 5) -> float:
    """Median per-call cost of `with op:` around nothing, minus an empty loop. Includes timer reads."""
    op = Op("calibration", "general", {}, slow_ms=None, span_sample_every=0)
    results = []
    for _ in range(repeats):
        t0 = _perf()
        for _ in range(iterations):
            pass
        empty = _perf() - t0
        t0 = _perf()
        for _ in range(iterations):
            with op:
                pass
        timed = _perf() - t0
        results.append(max(0.0, (timed - empty) / iterations))
    return statistics.median(results)
