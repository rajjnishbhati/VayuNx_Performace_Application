"""Per-thread CPU time with sub-millisecond resolution.

On Windows `time.thread_time_ns()` is GetThreadTimes(), which only advances on the scheduler tick
(~15.6 ms): an 8.7 ms PBKDF2 call measured 0.0 ms of CPU on an i5-8400H. There we use
QueryThreadCycleTime (TSC cycles charged to the thread) divided by the TSC rate, calibrated once against
perf_counter on a busy loop. Elsewhere thread_time_ns() already has microsecond resolution.
"""

from __future__ import annotations

import sys
import time

SOURCE = "thread_time_ns"
thread_cpu_ns = time.thread_time_ns

if sys.platform == "win32":
    try:
        import ctypes
        import threading

        _query = ctypes.windll.kernel32.QueryThreadCycleTime  # no argtypes: ~1.4 us per reading instead of ~4 us (i5-8400H)
        _handle = ctypes.c_void_p(-2)  # GetCurrentThread() pseudo-handle: always the calling thread
        _tls = threading.local()

        def _cycles() -> int:
            ref = getattr(_tls, "ref", None)
            if ref is None:
                _tls.buf = ctypes.c_ulonglong()
                ref = _tls.ref = ctypes.byref(_tls.buf)
            _query(_handle, ref)
            return _tls.buf.value

        def _calibrate() -> float:
            best = 0.0
            for _ in range(3):  # a preempted sample reads low, so keep the highest cycles/ns
                c0, t0 = _cycles(), time.perf_counter_ns()
                while time.perf_counter_ns() - t0 < 5_000_000:
                    pass
                c1, t1 = _cycles(), time.perf_counter_ns()
                best = max(best, (c1 - c0) / (t1 - t0))
            return best

        _cycles_per_ns = _calibrate()
        if _cycles_per_ns > 0.1:
            def thread_cpu_ns() -> int:  # noqa: F811
                return int(_cycles() / _cycles_per_ns)
            SOURCE = f"QueryThreadCycleTime ({_cycles_per_ns:.3f} cycles/ns)"
    except Exception:  # fall back to the coarse clock rather than fail
        pass
