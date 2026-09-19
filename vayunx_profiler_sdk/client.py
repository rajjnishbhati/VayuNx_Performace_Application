"""Instrumentation API (context managers) and run lifecycle.

Safety contract (spec A):
* Profiler internals never raise into host code: errors are logged once and counted in stats().
  Only API misuse (invalid phase/category/metric, nested runs) raises, immediately and clearly.
* No network I/O on the caller's thread: everything goes through the background sender.
* Starting a run never blocks on the service; if it is down, data is buffered (bounded) and retried.
* The host's own exception is never replaced or masked.
* Run exit flushes with a hard timeout (flush_timeout_s, default 2 s); so does process exit.

Nesting is automatic: the innermost open span is tracked in a ContextVar (per thread / asyncio
task). Spans opened in a *different* thread do not inherit a parent - they become roots.
"""

from __future__ import annotations

import itertools
import logging
import os
import platform
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from vayunx_profiler_sdk.context import current_span
from vayunx_profiler_sdk.ops import Op, measure_op_overhead_ns, span_times
from vayunx_profiler_sdk.privacy import clean_attributes
from vayunx_profiler_sdk.sampling import Sampler
from vayunx_profiler_sdk.sender import BackgroundSender
from vayunx_profiler_sdk.transport import HttpTransport

CATEGORIES = ("general", "cryptographic")
PHASES = ("baseline", "remediated")
log = logging.getLogger("vayunx_profiler")


def _check_category(category: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}, got {category!r}")


class ProfilerClient:
    def __init__(self, service_url: str, service_name: str, *, timeout: float = 5.0, transport=None,
                 batch_size: int = 500, flush_batch_size: int | None = None, max_queue: int = 10_000,
                 flush_interval_s: float = 1.0, flush_timeout_s: float = 2.0,
                 backoff_initial_s: float = 0.5, backoff_max_s: float = 30.0):
        from vayunx_profiler_sdk import __version__

        if not service_name:
            raise ValueError("service_name is required")
        self.service_name = service_name
        self.sdk_id = f"vayunx-profiler-sdk-python/{__version__}"
        self.flush_timeout_s = flush_timeout_s
        self._sender = BackgroundSender(
            transport or HttpTransport(service_url, timeout, user_agent=self.sdk_id),
            max_queue=max_queue, batch_size=flush_batch_size or batch_size, flush_interval_s=flush_interval_s,
            flush_timeout_s=flush_timeout_s, backoff_initial_s=backoff_initial_s, backoff_max_s=backoff_max_s,
            on_tick=self._drain_ops)
        self._lock = threading.Lock()
        self._active_run: ProfilerRun | None = None
        self._sampler: Sampler | None = None
        self._internal_errors = 0
        self._dropped_attributes = 0
        self._op_overhead_ns: float | None = None
        self._warned: set[str] = set()

    # -------------------------------------------------------------- runs

    def run(self, label: str, phase: str, *, run_id: str | None = None, metadata: dict | None = None) -> "ProfilerRun":
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}, got {phase!r}")
        return ProfilerRun(self, label, phase, run_id, metadata or {})

    @property
    def active_run(self) -> "ProfilerRun | None":
        return self._active_run

    # -------------------------------------------------------------- sampling

    def start_sampling(self, interval_ms: int = 500, metrics: Iterable[str] = ("cpu_pct", "memory_mb"),
                       category: str = "general") -> None:
        """Start a background thread sampling this process; samples are tagged to the active run."""
        _check_category(category)
        run = self._active_run
        if run is None:
            raise RuntimeError("start_sampling() needs an active run: call it inside `with profiler.run(...)`")
        if self._sampler is not None:
            raise RuntimeError("sampling is already running")
        self._sampler = Sampler(self._sender.enqueue, run.run_id, self.service_name, category, interval_ms, list(metrics))
        self._sampler.start()

    def stop_sampling(self) -> dict:
        """Stop sampling and take a final sample. Returns {'samples_recorded': n, 'errors': [...]}."""
        if self._sampler is None:
            raise RuntimeError("sampling is not running")
        sampler, self._sampler = self._sampler, None
        result = sampler.stop()
        if result["errors"]:
            self._warn_once("sampling", f"profiler sampling had {len(result['errors'])} error(s): {result['errors'][:3]}")
        return result

    # -------------------------------------------------------------- health / lifecycle

    def stats(self) -> dict:
        """Counters for everything recorded, sent and dropped. Never raises."""
        try:
            out = self._sender.stats()
        except Exception:
            out = {}
        with self._lock:
            out["internal_errors"] = out.get("internal_errors", 0) + self._internal_errors
            out["dropped_attributes"] = self._dropped_attributes
        out["op_overhead_ns"] = self._op_overhead_ns
        return out

    def flush(self, timeout: float | None = None) -> bool:
        try:
            return self._sender.flush(timeout)
        except Exception as exc:
            self._internal_error("flush", exc)
            return False

    def close(self, timeout: float | None = None) -> bool:
        return self.flush(timeout)

    # -------------------------------------------------------------- internals

    def _clean(self, attributes: dict | None) -> dict:
        clean, dropped = clean_attributes(attributes)
        if dropped:
            with self._lock:
                self._dropped_attributes += dropped
            self._warn_once("attributes", "profiler dropped sensitive or unsupported attributes (see stats()['dropped_attributes'])")
        return clean

    def _internal_error(self, where: str, exc: BaseException) -> None:
        with self._lock:
            self._internal_errors += 1
        self._warn_once(f"internal-{where}", f"profiler internal error in {where}: {exc!r}")

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message)

    def _drain_ops(self) -> None:
        run = self._active_run
        if run is not None:
            run._drain_ops()

    def _calibrate_ops(self) -> None:
        if self._op_overhead_ns is None:
            self._op_overhead_ns = measure_op_overhead_ns()


class ProfilerRun:
    def __init__(self, client: ProfilerClient, label: str, phase: str, run_id: str | None, metadata: dict):
        self.client, self.label, self.phase = client, label, phase
        self.run_id: str | None = None
        self._requested_run_id = run_id
        self.metadata = {"sdk": client.sdk_id, "runtime": f"python {platform.python_version()}",
                         "platform": platform.platform(), "hostname": socket.gethostname(), "pid": os.getpid(),
                         "cpu_count": os.cpu_count(), **client._clean(metadata)}
        self._lock = threading.Lock()
        self._open_spans = 0
        self._closed = False
        self._ops: list[Op] = []

    # ---- delivery counters (per run, what the service has accepted)
    @property
    def spans_sent(self) -> int:
        return self.client._sender.delivered(self.run_id, "spans") if self.run_id else 0

    @property
    def samples_sent(self) -> int:
        return self.client._sender.delivered(self.run_id, "samples") if self.run_id else 0

    @property
    def op_stats_sent(self) -> int:
        return self.client._sender.delivered(self.run_id, "op_stats") if self.run_id else 0

    def __enter__(self) -> "ProfilerRun":
        with self.client._lock:
            if self.client._active_run is not None:
                raise RuntimeError("another run is already active on this ProfilerClient")
            self.client._active_run = self
        self.run_id = self._requested_run_id or uuid.uuid4().hex  # client-side id: no round trip needed
        try:
            self.client._sender.enqueue("run", self.run_id, {
                "run_id": self.run_id, "service": self.client.service_name, "label": self.label,
                "phase": self.phase, "metadata": self.metadata})
        except Exception as exc:
            self.client._internal_error("run-start", exc)
        return self

    def span(self, name: str, category: str = "general", attributes: dict | None = None) -> "SpanContext":
        _check_category(category)
        if self.run_id is None or self._closed:
            raise RuntimeError("span() must be used inside an active `with profiler.run(...)` block")
        return SpanContext(self, name, category, self.client._clean(attributes))

    def op(self, name: str, category: str = "cryptographic", attributes: dict | None = None, *,
           slow_ms: float | None = 10.0, span_sample_every: int = 0) -> Op:
        """Fast-path timer: per-call latency histogram, one summary per interval (see ops.py)."""
        _check_category(category)
        if self.run_id is None or self._closed:
            raise RuntimeError("op() must be used inside an active `with profiler.run(...)` block")
        try:
            self.client._calibrate_ops()
        except Exception as exc:
            self.client._internal_error("calibrate", exc)
        op = Op(name, category, self.client._clean(attributes), slow_ms, span_sample_every,
                on_span=self._op_span, on_error=self.client._internal_error)
        with self._lock:
            self._ops.append(op)
        return op

    # ---- internals
    def _record(self, kind: str, payload: dict) -> None:
        try:
            self.client._sender.enqueue(kind, self.run_id, payload)
        except Exception as exc:
            self.client._internal_error(f"record-{kind}", exc)

    def _span_payload(self, span_id, parent_span_id, name, category, start_iso, end_iso, duration_ms, attributes) -> dict:
        return {"run_id": self.run_id, "service": self.client.service_name, "category": category, "span_id": span_id,
                "parent_span_id": parent_span_id, "span_name": name, "start_time": start_iso, "end_time": end_iso,
                "duration_ms": duration_ms, "attributes": attributes}

    def _op_span(self, op: Op, elapsed_ns: int, reason: str, exc_type) -> None:
        current = current_span.get()
        parent = current[1] if current and current[0] == self.run_id else None
        start, end = span_times(elapsed_ns)
        attrs = {**op.attributes, "vayunx.sampled": reason}
        if exc_type is not None:
            attrs["error"] = exc_type.__name__
        self._record("span", self._span_payload(_new_span_id(), parent, op.name, op.category, start, end,
                                                elapsed_ns / 1e6, attrs))

    def _drain_ops(self) -> None:
        with self._lock:
            ops = list(self._ops)
        for op in ops:
            try:
                drained = op.drain()
                if drained is None:
                    continue
                hist, start, end = drained
                self._record("op_stats", {
                    "run_id": self.run_id, "service": self.client.service_name, "category": op.category,
                    "op_name": op.name, "attributes": op.attributes,
                    "interval_start": start.isoformat(), "interval_end": end.isoformat(),
                    "count": hist.count, "sum_ns": hist.sum_ns, "min_ns": hist.min_ns, "max_ns": hist.max_ns,
                    "p50_ns": hist.percentile(50), "p95_ns": hist.percentile(95), "p99_ns": hist.percentile(99),
                    "histogram": hist.to_dict(), "sdk_overhead_ns": self.client._op_overhead_ns,
                })
            except Exception as exc:
                self.client._internal_error("drain-ops", exc)

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            sampler = self.client._sampler
            if sampler is not None and sampler.run_id == self.run_id:
                self.client.stop_sampling()
            self._drain_ops()
            if self._open_spans:
                self.client._warn_once("open-spans", f"run {self.run_id} closed with {self._open_spans} span(s) still open")
            self._record("complete", {})
            self.client.flush()
        except Exception as err:  # profiler failures must never surface in, or replace, host exceptions
            self.client._internal_error("run-exit", err)
        finally:
            self._closed = True
            with self.client._lock:
                self.client._active_run = None
        return False  # never swallow the caller's exception


_SPAN_PREFIX = os.urandom(6).hex()  # per process; plus a counter -> unique within any run, far cheaper than uuid4
_span_counter = itertools.count(1)


def _new_span_id() -> str:
    return f"{_SPAN_PREFIX}{next(_span_counter):x}"


class SpanContext:
    def __init__(self, run: ProfilerRun, name: str, category: str, attributes: dict):
        self.run, self.name, self.category, self.attributes = run, name, category, attributes
        self.span_id = _new_span_id()
        self.parent_span_id: str | None = None
        self.duration_ms: float | None = None

    def __enter__(self) -> "SpanContext":
        current = current_span.get()
        self.parent_span_id = current[1] if current and current[0] == self.run.run_id else None
        self._token = current_span.set((self.run.run_id, self.span_id))
        with self.run._lock:
            self.run._open_spans += 1
        self._start_wall = datetime.now(timezone.utc)
        self._t0 = time.perf_counter_ns()  # monotonic, high resolution; wall clock only anchors start_time
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed_ns = time.perf_counter_ns() - self._t0
        try:
            current_span.reset(self._token)
            with self.run._lock:
                self.run._open_spans -= 1
            self.duration_ms = elapsed_ns / 1e6
            attrs = dict(self.attributes)
            if exc_type is not None:
                attrs["error"] = exc_type.__name__
            end_wall = self._start_wall + timedelta(microseconds=elapsed_ns / 1000)  # keeps end - start == duration
            # datetimes are serialised by the sender thread, not here
            self.run._record("span", self.run._span_payload(
                self.span_id, self.parent_span_id, self.name, self.category,
                self._start_wall, end_wall, self.duration_ms, attrs))
        except Exception as err:
            self.run.client._internal_error("span-exit", err)
        return False
