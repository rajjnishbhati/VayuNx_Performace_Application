"""Instrumentation API (context managers) and run lifecycle.

Nesting is automatic: the currently open span is tracked in a ContextVar, so a span opened
inside another open span becomes its child (parent_span_id) with no IDs passed by the caller.
Scope of that tracking: the current thread / asyncio task. Spans opened in a *different*
thread do not inherit a parent (ContextVars are not copied into new threads) - they become roots.
"""

from __future__ import annotations

import os
import platform
import socket
import threading
import time
import uuid
import warnings
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from vayunx_profiler_sdk.sampling import Sampler
from vayunx_profiler_sdk.transport import HttpTransport

CATEGORIES = ("general", "cryptographic")
PHASES = ("baseline", "remediated")

# (run_id, span_id) of the innermost open span in this thread/task
_current_span: ContextVar[tuple[str, str] | None] = ContextVar("vayunx_profiler_current_span", default=None)


def _check_category(category: str) -> None:
    if category not in CATEGORIES:
        raise ValueError(f"category must be one of {CATEGORIES}, got {category!r}")


class ProfilerClient:
    def __init__(self, service_url: str, service_name: str, *, timeout: float = 10.0,
                 flush_batch_size: int = 500, transport: Any = None):
        from vayunx_profiler_sdk import __version__

        if not service_name:
            raise ValueError("service_name is required")
        self.service_name = service_name
        self.flush_batch_size = flush_batch_size
        self.sdk_id = f"vayunx-profiler-sdk-python/{__version__}"
        self._transport = transport or HttpTransport(service_url, timeout, user_agent=self.sdk_id)
        self._lock = threading.Lock()
        self._active_run: ProfilerRun | None = None
        self._sampler: Sampler | None = None

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
        self._sampler = Sampler(self._transport, run, self.service_name, category, interval_ms, list(metrics))
        self._sampler.start()

    def stop_sampling(self) -> dict:
        """Stop sampling, take a final sample, flush. Returns {'samples_sent': n, 'errors': [...]}."""
        if self._sampler is None:
            raise RuntimeError("sampling is not running")
        sampler, self._sampler = self._sampler, None
        result = sampler.stop()
        if result["errors"]:
            warnings.warn(f"profiler sampling had {len(result['errors'])} error(s): {result['errors'][:3]}", RuntimeWarning, stacklevel=2)
        return result


class ProfilerRun:
    def __init__(self, client: ProfilerClient, label: str, phase: str, run_id: str | None, metadata: dict):
        self.client, self.label, self.phase = client, label, phase
        self.requested_run_id = run_id
        self.run_id: str | None = None
        self.metadata = {"sdk": client.sdk_id, "runtime": f"python {platform.python_version()}",
                         "platform": platform.platform(), "hostname": socket.gethostname(), "pid": os.getpid(),
                         "cpu_count": os.cpu_count(), **metadata}
        self._buffer: list[dict] = []
        self._buffer_lock = threading.Lock()
        self._open_spans = 0
        self._closed = False
        self.spans_sent = 0

    def __enter__(self) -> "ProfilerRun":
        with self.client._lock:
            if self.client._active_run is not None:
                raise RuntimeError("another run is already active on this ProfilerClient")
            self.client._active_run = self
        try:
            body = {"service": self.client.service_name, "label": self.label, "phase": self.phase, "metadata": self.metadata}
            if self.requested_run_id:
                body["run_id"] = self.requested_run_id
            self.run_id = self.client._transport.post("/v1/runs", body)["run_id"]
        except BaseException:
            self.client._active_run = None
            raise
        return self

    def span(self, name: str, category: str = "general", attributes: dict | None = None) -> "SpanContext":
        _check_category(category)
        if self.run_id is None or self._closed:
            raise RuntimeError("span() must be used inside an active `with profiler.run(...)` block")
        return SpanContext(self, name, category, attributes or {})

    def _record(self, span: dict) -> None:
        with self._buffer_lock:
            self._buffer.append(span)
            full = len(self._buffer) >= self.client.flush_batch_size
        if full:
            self.flush()

    def flush(self) -> None:
        with self._buffer_lock:
            batch, self._buffer = self._buffer, []
        for i in range(0, len(batch), self.client.flush_batch_size):
            chunk = batch[i:i + self.client.flush_batch_size]
            self.client._transport.post("/v1/spans", chunk)
            self.spans_sent += len(chunk)

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            sampler = self.client._sampler
            if sampler is not None and sampler.run is self:
                self.client.stop_sampling()
            if self._open_spans:
                warnings.warn(f"run {self.run_id} closed with {self._open_spans} span(s) still open", RuntimeWarning, stacklevel=2)
            self.flush()
            self.client._transport.post(f"/v1/runs/{self.run_id}/complete", {})
        finally:
            self._closed = True
            self.client._active_run = None
        return False  # never swallow the caller's exception


class SpanContext:
    def __init__(self, run: ProfilerRun, name: str, category: str, attributes: dict):
        self.run, self.name, self.category, self.attributes = run, name, category, dict(attributes)
        self.span_id = uuid.uuid4().hex
        self.parent_span_id: str | None = None
        self.duration_ms: float | None = None

    def __enter__(self) -> "SpanContext":
        current = _current_span.get()
        self.parent_span_id = current[1] if current and current[0] == self.run.run_id else None
        self._token = _current_span.set((self.run.run_id, self.span_id))
        self.run._open_spans += 1
        self._start_wall = datetime.now(timezone.utc)
        self._t0 = time.perf_counter_ns()  # monotonic, high resolution; wall clock only anchors start_time
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        elapsed_ns = time.perf_counter_ns() - self._t0
        _current_span.reset(self._token)
        self.run._open_spans -= 1
        self.duration_ms = elapsed_ns / 1e6
        attrs = dict(self.attributes)
        if exc_type is not None:
            attrs["error"] = exc_type.__name__
        end_wall = self._start_wall + timedelta(microseconds=elapsed_ns / 1000)  # keeps end - start == duration
        self.run._record({
            "run_id": self.run.run_id, "service": self.run.client.service_name, "category": self.category,
            "span_id": self.span_id, "parent_span_id": self.parent_span_id, "span_name": self.name,
            "start_time": self._start_wall.isoformat(), "end_time": end_wall.isoformat(),
            "duration_ms": self.duration_ms, "attributes": attrs,
        })
        return False
