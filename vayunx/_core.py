"""SDK state, the per-call sink the hooks report to, and the public init/shutdown/span/measure/status API."""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import functools
import inspect
import logging
import threading
import time
from typing import Callable

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from vayunx import _config
from vayunx._cpuclock import SOURCE as CPU_CLOCK_SOURCE
from vayunx._cpuclock import thread_cpu_ns
from vayunx._hooks import HookManager, Spec, key_id
from vayunx._otel import CPU_ATTR, meter_provider, resource_attrs, tracer_provider
from vayunx._ship import OpHistograms, Shipper
from vayunx_profiler_sdk.histogram import LatencyHistogram
from vayunx_profiler_sdk.sampling import Activity

log = logging.getLogger("vayunx")
_get_running_loop = asyncio._get_running_loop  # 132 ns; the public get_running_loop() raises when absent


class _State:
    def __init__(self, cfg: _config.Config, version: str):
        self.cfg = cfg
        self.counters: dict[str, int] = {"spans": 0, "errors": 0, "internal_errors": 0}
        self.findings = {"blocking_event_loop": 0}
        self.activity = Activity()
        self.ophists = OpHistograms()
        self.hist_lock, self.hists = self.ophists.lock, self.ophists.hists
        attrs = resource_attrs(cfg, version)
        self.tp = tracer_provider(cfg, attrs)
        self.tracer = self.tp.get_tracer("vayunx-python", version)
        self.mp = meter_provider(cfg, attrs, self.activity) if cfg.gauges else None
        self.shipper = Shipper(cfg, attrs, self.ophists, self.counters)
        self.slow_ns = int(cfg.slow_ms * 1_000_000)
        self.sample_every = cfg.span_sample_every
        self._sampled = 0

    def new_hist(self, kid: int, ns: int) -> LatencyHistogram:  # caller holds hist_lock
        h = self.hists[kid] = LatencyHistogram()
        h.min_ns = h.max_ns = ns
        return h

    # called after every hooked call that is not on the inlined leaf fast path (see _hooks.py)
    def on_call(self, spec: Spec, args, kwargs, t0: int, t1: int, c0: int, err: BaseException | None) -> None:
        ns = t1 - t0
        act = self.activity
        act.last_op_ns = t1
        if act.idle:
            act.wake()
        if err is not None:
            self.counters["errors"] += 1
            if spec.slow:
                self._span(spec, args, kwargs, ns, c0, err)
            return
        ck = spec.fixed or (spec.keyfn(args, kwargs) if spec.keyfn else spec.describe(args, kwargs)[:2])
        kid = spec.keys.get(ck)
        if kid is None:
            kid = spec.keys[ck] = key_id(spec, *ck)
        with self.hist_lock:
            h = self.hists.get(kid)
            if h is None:
                h = self.new_hist(kid, ns)
            h.record(ns)
        if ns >= self.slow_ns or self.sample_every:
            self.on_slow_or_sampled(spec, args, kwargs, ns, c0)

    def on_slow_or_sampled(self, spec, args, kwargs, ns: int, c0: int) -> None:
        if ns >= self.slow_ns:
            self._span(spec, args, kwargs, ns, c0, None)
            return
        self._sampled += 1
        if self._sampled % self.sample_every == 0:
            self._span(spec, args, kwargs, ns, c0, None)

    def _span(self, spec, args, kwargs, ns, c0, err) -> None:
        end = time.time_ns()
        algorithm, params, input_bytes = spec.describe(args, kwargs)
        attrs = {"crypto.operation": spec.operation, "crypto.algorithm": algorithm, "crypto.library": spec.library,
                 "crypto.sync": True}
        if params:
            attrs["crypto.params"] = params
        if input_bytes is not None:
            attrs["crypto.input_bytes"] = input_bytes
        if c0:
            attrs[CPU_ATTR] = max(0.0, (thread_cpu_ns() - c0) / 1e6)
        if _get_running_loop() is not None and ns >= self.slow_ns:
            self.findings["blocking_event_loop"] += 1
            attrs["vayunx.finding"] = "blocking_event_loop"
        span = self.tracer.start_span(f"{spec.operation} {algorithm}", start_time=end - ns, attributes=attrs)
        if err is not None:
            span.set_status(Status(StatusCode.ERROR, type(err).__name__))  # the type only; messages may echo input
        span.end(end_time=end)
        self.counters["spans"] += 1

    def internal_error(self) -> None:
        self.counters["internal_errors"] += 1


_lock = threading.RLock()
_state: _State | None = None
_cell: list = [None]  # what hook wrappers read: [_state]
_hooks: HookManager | None = None
_last_status: dict = {}
_atexit_registered = False


def init(endpoint: str | None = None, service: str | None = None, variant: str | None = None,
         run_id: str | None = None, **options) -> dict:
    """Start profiling this process. Safe to call more than once (later calls return the status).
    Never raises: on any failure the app keeps running unprofiled and the status says why."""
    global _state, _hooks, _atexit_registered
    from vayunx import __version__
    with _lock:
        if _state is not None:
            return status()
        if _config.disabled():
            return {"initialized": False, "reason": "VAYUNX_DISABLE is set"}
        try:
            cfg = _config.load(endpoint=endpoint, service=service, variant=variant, run_id=run_id, **options)
            st = _State(cfg, __version__)
            st.shipper.start()
            hooks = HookManager(_cell)
            if cfg.hooks:
                hooks.install()
            _state, _hooks = st, hooks
            _cell[0] = st
            if not _atexit_registered:
                atexit.register(shutdown)
                _atexit_registered = True
        except Exception as exc:
            log.warning("vayunx: profiling disabled (%s: %s)", type(exc).__name__, exc)
            return {"initialized": False, "reason": f"{type(exc).__name__}: {exc}"}
        return status()


def shutdown(timeout_s: float = 2.0) -> bool:
    """Restore every patched function, then flush telemetry with a hard deadline. True = everything sent."""
    global _state, _hooks, _last_status
    with _lock:
        st, hooks = _state, _hooks
        if st is None:
            return True
        _last_status = status()
        _state = None
        _cell[0] = None  # wrappers become pass-throughs from here on
        _hooks = None
    if hooks is not None:
        hooks.uninstall()
    deadline = time.monotonic() + timeout_s
    result = {"otel": False}

    def flush_otel():
        ms = int(timeout_s * 1000)
        ok = st.tp.force_flush(ms)
        st.tp.shutdown()
        if st.mp is not None:
            st.mp.shutdown(timeout_millis=ms)
        result["otel"] = bool(ok)

    t = threading.Thread(target=flush_otel, name="vayunx-otel-flush", daemon=True)
    t.start()
    shipped = st.shipper.stop(timeout_s)
    t.join(max(0.0, deadline - time.monotonic()))
    return bool(shipped and result["otel"] and not t.is_alive())


def status() -> dict:
    st, hooks = _state, _hooks
    if st is None:
        return {"initialized": False, **({"last_run": _last_status} if _last_status else {})}
    cfg = st.cfg
    return {"initialized": True, "service": cfg.service, "variant": cfg.variant, "run_id": cfg.run_id,
            "endpoint": cfg.endpoint, "phase": cfg.phase, "slow_ms": cfg.slow_ms,
            "hooks": dict(hooks.status) if hooks else {}, "findings": dict(st.findings),
            "counters": dict(st.counters), "cpu_clock": CPU_CLOCK_SOURCE}


def original(name: str) -> Callable | None:
    """The unpatched function behind hook `name` (for overhead measurements)."""
    hooks = _hooks
    return hooks.original(name) if hooks else None


class _NoopSpan(contextlib.AbstractContextManager):
    def __exit__(self, *exc):
        return False


def span(name: str, **attributes):
    """Context manager: a named span around a unit of work (crypto spans inside become its children).
    Attributes are scrubbed of secrets by the receiver; pass sizes and parameters only."""
    st = _state
    if st is None:
        return _NoopSpan()
    return _SpanCM(st, name, attributes)


class _SpanCM:
    __slots__ = ("st", "name", "attributes", "_cm")

    def __init__(self, st, name, attributes):
        self.st, self.name, self.attributes, self._cm = st, name, attributes, None

    def __enter__(self):
        try:
            self._cm = self.st.tracer.start_as_current_span(self.name, attributes=self.attributes or None,
                                                            record_exception=False, set_status_on_exception=False)
            return self._cm.__enter__()
        except Exception:
            self._cm = None
            return None

    def __exit__(self, exc_type, exc, tb):
        cm = self._cm
        if cm is None:
            return False
        try:
            if exc_type is not None:
                trace.get_current_span().set_status(Status(StatusCode.ERROR, exc_type.__name__))
            cm.__exit__(None, None, None)
        except Exception:
            pass
        return False  # never swallow the host's exception


def measure(name: str | None = None, **attributes):
    """Decorator: wrap every call of a function (sync or async) in a span."""
    def decorate(fn):
        label = name or fn.__qualname__
        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                with span(label, **attributes):
                    return await fn(*args, **kwargs)
            return async_wrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            with span(label, **attributes):
                return fn(*args, **kwargs)
        return wrapper

    if callable(name):  # used bare: @vayunx.measure
        fn, name = name, None
        return decorate(fn)
    return decorate
