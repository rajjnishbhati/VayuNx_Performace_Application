"""Background sampling of *this process's* system metrics via psutil.

"Sampling" in this project = periodic system-resource metrics, NOT statistical call-stack
sampling (py-spy / async-profiler style). See README "Terminology".

Timing of samples:
* memory_mb / num_threads: one sample at start, one per interval, one at stop.
* cpu_pct: psutil measures CPU since the previous call, so the start call only primes the
  counter (its value is meaningless and is discarded); then one sample per interval and a
  final one at stop covering the time since the last sample.
So even a run shorter than one interval gets start/stop samples - but the report flags metrics
with fewer than 3 samples as indicative only.

Overhead: the sampler thread (psutil calls + periodic batched HTTP flush) runs inside the
measured process, so its own CPU/memory is included in what it measures. Samples are buffered
and flushed every ~2 s rather than one HTTP call per sample.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import psutil

if TYPE_CHECKING:
    from vayunx_profiler_sdk.client import ProfilerRun

SUPPORTED_METRICS = {
    "cpu_pct": "%",  # percent of ONE logical CPU (can exceed 100 when several threads run)
    "memory_mb": "MiB",  # resident set size (RSS)
    "num_threads": "count",
}
MIN_INTERVAL_MS = 10
FLUSH_EVERY_S = 2.0


class Sampler:
    def __init__(self, transport: Any, run: "ProfilerRun", service: str, category: str, interval_ms: int, metrics: list[str]):
        unknown = [m for m in metrics if m not in SUPPORTED_METRICS]
        if unknown:
            raise ValueError(f"unsupported metrics {unknown}; supported: {sorted(SUPPORTED_METRICS)}")
        if not metrics:
            raise ValueError("at least one metric is required")
        if interval_ms < MIN_INTERVAL_MS:
            raise ValueError(f"interval_ms must be >= {MIN_INTERVAL_MS}")
        self.transport, self.run, self.service, self.category = transport, run, service, category
        self.interval_s = interval_ms / 1000.0
        self.metrics = list(dict.fromkeys(metrics))
        self._proc = psutil.Process()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._buffer: list[dict] = []
        self.samples_sent = 0
        self.errors: list[str] = []
        self._thread = threading.Thread(target=self._loop, name="vayunx-profiler-sampler", daemon=True)

    def start(self) -> None:
        if "cpu_pct" in self.metrics:
            self._proc.cpu_percent(None)  # prime; this first reading is meaningless and discarded
        self._take(initial=True)
        self._thread.start()

    def _take(self, initial: bool = False) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        rows = []
        with self._proc.oneshot():
            for metric in self.metrics:
                if metric == "cpu_pct":
                    if initial:
                        continue
                    value = self._proc.cpu_percent(None)
                elif metric == "memory_mb":
                    value = self._proc.memory_info().rss / (1024 * 1024)
                else:
                    value = self._proc.num_threads()
                rows.append({"run_id": self.run.run_id, "service": self.service, "category": self.category,
                             "metric_name": metric, "value": float(value), "unit": SUPPORTED_METRICS[metric], "timestamp": ts})
        with self._lock:
            self._buffer.extend(rows)

    def _flush(self) -> None:
        with self._lock:
            batch, self._buffer = self._buffer, []
        if not batch:
            return
        try:
            self.transport.post("/v1/samples", batch)
            self.samples_sent += len(batch)
        except Exception as exc:  # never crash the instrumented application
            self.errors.append(repr(exc))

    def _loop(self) -> None:
        last_flush = time.monotonic()
        while not self._stop.wait(self.interval_s):
            try:
                self._take()
            except Exception as exc:
                self.errors.append(repr(exc))
            if time.monotonic() - last_flush >= FLUSH_EVERY_S:
                self._flush()
                last_flush = time.monotonic()

    def stop(self) -> dict:
        self._stop.set()
        self._thread.join()
        try:
            self._take()  # final sample: covers the time since the last periodic sample
        except Exception as exc:
            self.errors.append(repr(exc))
        self._flush()
        return {"samples_sent": self.samples_sent, "errors": list(self.errors)}
