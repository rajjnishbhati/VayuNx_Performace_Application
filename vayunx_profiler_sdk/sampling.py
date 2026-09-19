"""Background sampling (the machine lens) - spec C.

"Sampling" in this project = periodic system-resource metrics, NOT statistical call-stack
sampling (py-spy / async-profiler style). See README "Terminology".

Two jobs:
* Cost - this process: cores busy, CPU time, RSS (and peak RSS on Windows), threads, context switches.
* Noise check - the whole machine: CPU % (average and busiest core), available RAM, swap, load,
  process count, CPU frequency, and "other cores busy" (machine CPU not used by this process).

Adaptive interval (default): about 10 ms while a cryptographic span or op is active, about 1 s
otherwise. Crypto activity wakes the sampler immediately. Machine metrics are read at most every
250 ms (process count and CPU frequency about every second) because they cost more. Samples taken
while crypto is running are tagged category "cryptographic", the rest "general", and use the same
UTC wall clock as spans - so a UI can shade "crypto was running here".

The sampler times its own work (overhead_ms_per_tick). It runs inside the measured process, so its
cost is included in what it measures.

v1 compatibility: metric names cpu_pct / memory_mb / num_threads, a fixed interval_ms, or a fixed
category keep the original behaviour.

psutil failures are collected in `errors` and never raised into host code. Samples go to the
client's background sender (no network I/O here).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import Callable

from vayunx_profiler_sdk.metrics import UNITS, MachineReader, ProcessReader

V1_METRICS = {
    "cpu_pct": "%",  # percent of ONE logical CPU (can exceed 100 when several threads run)
    "memory_mb": "MiB",  # resident set size (RSS)
    "num_threads": "count",
}
PROCESS_METRICS = ("proc_cores_busy", "proc_cpu_time_s", "proc_rss_mib", "proc_peak_rss_mib", "proc_threads", "proc_ctx_switches")
MACHINE_METRICS = ("machine_cpu_pct", "machine_cpu_pct_max_core", "machine_mem_available_mib", "machine_swap_used_mib",
                   "machine_load1", "machine_proc_count", "machine_cpu_freq_mhz", "machine_other_cores_busy")
SUPPORTED_METRICS = {**V1_METRICS, **{m: UNITS[m] for m in PROCESS_METRICS + MACHINE_METRICS}}
DEFAULT_METRICS = PROCESS_METRICS + MACHINE_METRICS
MIN_INTERVAL_MS = 10
FAST_INTERVAL_S = 0.010
SLOW_INTERVAL_S = 1.0
MACHINE_EVERY_S = 0.25
MACHINE_FULL_EVERY_S = 1.0
ACTIVE_WINDOW_NS = 50_000_000  # an op finished within the last 50 ms counts as crypto activity


class Activity:
    """Shared crypto-activity signal between a run (spans/ops) and its sampler."""

    __slots__ = ("open_crypto_spans", "last_op_ns", "idle", "event", "_lock")

    def __init__(self):
        self.open_crypto_spans = 0
        self.last_op_ns = 0
        self.idle = False
        self.event = threading.Event()
        self._lock = threading.Lock()

    def span_enter(self) -> None:
        with self._lock:
            self.open_crypto_spans += 1
        self.wake()

    def span_exit(self) -> None:
        with self._lock:
            self.open_crypto_spans -= 1

    def op_done(self, end_ns: int) -> None:  # hot path: two attribute stores in the common case
        self.last_op_ns = end_ns
        if self.idle:
            self.wake()

    def wake(self) -> None:
        self.idle = False
        self.event.set()

    def active(self) -> bool:
        return self.open_crypto_spans > 0 or time.perf_counter_ns() - self.last_op_ns < ACTIVE_WINDOW_NS


class Sampler:
    def __init__(self, emit: Callable[[str, str, dict], bool], run_id: str, service: str, category: str | None,
                 interval_ms: int | None, metrics: list[str] | None, activity: Activity | None = None):
        metrics = list(dict.fromkeys(metrics)) if metrics else list(DEFAULT_METRICS)
        unknown = [m for m in metrics if m not in SUPPORTED_METRICS]
        if unknown:
            raise ValueError(f"unsupported metrics {unknown}; supported: {sorted(SUPPORTED_METRICS)}")
        if interval_ms is not None and interval_ms < MIN_INTERVAL_MS:
            raise ValueError(f"interval_ms must be >= {MIN_INTERVAL_MS}")
        self.emit, self.run_id, self.service = emit, run_id, service
        self.fixed_category = category
        self.fixed_interval_s = None if interval_ms is None else interval_ms / 1000.0
        self.metrics = metrics
        self._wanted = set(metrics)
        self._machine_wanted = bool(self._wanted & set(MACHINE_METRICS))
        self.activity = activity or Activity()
        self._stop = threading.Event()
        self.samples_recorded = 0
        self.errors: list[str] = []
        self.ticks = 0
        self._overhead_s = 0.0
        self._proc: ProcessReader | None = None
        self._machine: MachineReader | None = None
        self._last_machine = 0.0
        self._last_full = 0.0
        self._last_machine_values: dict = {}
        self._thread = threading.Thread(target=self._loop, name="vayunx-profiler-sampler", daemon=True)

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        try:
            self._proc = ProcessReader()
            self._proc.prime()  # the first CPU reading is meaningless; this starts the interval
            if self._machine_wanted:
                self._machine = MachineReader()
                self._machine.prime()
            self._take(initial=True)
        except Exception as exc:
            self.errors.append(repr(exc))
        self._thread.start()

    def stop(self) -> dict:
        self._stop.set()
        self.activity.event.set()
        self._thread.join(timeout=(self.fixed_interval_s or SLOW_INTERVAL_S) + 1.0)
        try:
            self._take()  # final sample: covers the time since the last periodic sample
        except Exception as exc:
            self.errors.append(repr(exc))
        return {"samples_recorded": self.samples_recorded, "errors": list(self.errors), "ticks": self.ticks,
                "overhead_ms_per_tick": (self._overhead_s / self.ticks * 1000) if self.ticks else 0.0}

    # ------------------------------------------------------------------ loop

    def _loop(self) -> None:
        act = self.activity
        while not self._stop.is_set():
            if self.fixed_interval_s is not None:
                interval = self.fixed_interval_s
            else:
                busy = act.active()
                interval = FAST_INTERVAL_S if busy else SLOW_INTERVAL_S
                act.idle = not busy
            act.event.wait(interval)
            act.event.clear()
            if self._stop.is_set():
                break
            try:
                self._take()
            except Exception as exc:
                if len(self.errors) < 100:
                    self.errors.append(repr(exc))

    def _take(self, initial: bool = False) -> None:
        if self._proc is None:
            raise RuntimeError("psutil process handle unavailable")
        t0 = time.perf_counter()
        ts = datetime.now(timezone.utc)
        category = self.fixed_category or ("cryptographic" if self.activity.active() else "general")
        # Expensive process metrics (threads, context switches) follow the slower machine cadence.
        slow_due = initial or t0 - self._last_machine >= MACHINE_EVERY_S or self.fixed_interval_s is not None
        p = self._proc.read(full=slow_due or "num_threads" in self._wanted)
        values: dict[str, float] = {}
        if "cpu_pct" in self._wanted and not initial:
            values["cpu_pct"] = p["proc_cores_busy"] * 100.0
        if "memory_mb" in self._wanted:
            values["memory_mb"] = p["proc_rss_mib"]
        if "num_threads" in self._wanted:
            values["num_threads"] = p["proc_threads"]
        for name in PROCESS_METRICS:
            if name in self._wanted and name in p and not (initial and name == "proc_cores_busy"):
                values[name] = p[name]
        if slow_due and self.fixed_interval_s is None:
            self._last_machine = t0
        if self._machine is not None and slow_due:
            full = initial or t0 - self._last_full >= MACHINE_FULL_EVERY_S
            m = self._machine.read(full=full)
            self._last_machine = t0
            if full:
                self._last_full = t0
            if not initial:
                m["machine_other_cores_busy"] = self._machine.other_cores_busy(m, p)
            for name, value in m.items():
                if name in self._wanted:
                    values[name] = value
        for name, value in values.items():
            self.emit("sample", self.run_id, {"run_id": self.run_id, "service": self.service, "category": category,
                                              "metric_name": name, "value": float(value),
                                              "unit": SUPPORTED_METRICS[name], "timestamp": ts})
            self.samples_recorded += 1
        if not initial:
            self.ticks += 1
            self._overhead_s += time.perf_counter() - t0
