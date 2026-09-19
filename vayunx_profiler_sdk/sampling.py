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

Samples are handed to the client's background sender (no network I/O here). psutil failures
are collected in `errors` and never raised into host code.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable

import psutil

SUPPORTED_METRICS = {
    "cpu_pct": "%",  # percent of ONE logical CPU (can exceed 100 when several threads run)
    "memory_mb": "MiB",  # resident set size (RSS)
    "num_threads": "count",
}
MIN_INTERVAL_MS = 10


class Sampler:
    def __init__(self, emit: Callable[[str, str, dict], bool], run_id: str, service: str, category: str,
                 interval_ms: int, metrics: list[str]):
        unknown = [m for m in metrics if m not in SUPPORTED_METRICS]
        if unknown:
            raise ValueError(f"unsupported metrics {unknown}; supported: {sorted(SUPPORTED_METRICS)}")
        if not metrics:
            raise ValueError("at least one metric is required")
        if interval_ms < MIN_INTERVAL_MS:
            raise ValueError(f"interval_ms must be >= {MIN_INTERVAL_MS}")
        self.emit, self.run_id, self.service, self.category = emit, run_id, service, category
        self.interval_s = interval_ms / 1000.0
        self.metrics = list(dict.fromkeys(metrics))
        self._stop = threading.Event()
        self.samples_recorded = 0
        self.errors: list[str] = []
        self._proc = None
        self._thread = threading.Thread(target=self._loop, name="vayunx-profiler-sampler", daemon=True)

    def start(self) -> None:
        try:
            self._proc = psutil.Process()
            if "cpu_pct" in self.metrics:
                self._proc.cpu_percent(None)  # prime; this first reading is meaningless and discarded
            self._take(initial=True)
        except Exception as exc:
            self.errors.append(repr(exc))
        self._thread.start()

    def _take(self, initial: bool = False) -> None:
        if self._proc is None:
            raise RuntimeError("psutil process handle unavailable")
        ts = datetime.now(timezone.utc).isoformat()
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
                self.emit("sample", self.run_id, {"run_id": self.run_id, "service": self.service, "category": self.category,
                                                  "metric_name": metric, "value": float(value),
                                                  "unit": SUPPORTED_METRICS[metric], "timestamp": ts})
                self.samples_recorded += 1

    def _loop(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                self._take()
            except Exception as exc:
                if len(self.errors) < 100:
                    self.errors.append(repr(exc))

    def stop(self) -> dict:
        self._stop.set()
        self._thread.join(timeout=self.interval_s + 1.0)
        try:
            self._take()  # final sample: covers the time since the last periodic sample
        except Exception as exc:
            self.errors.append(repr(exc))
        return {"samples_recorded": self.samples_recorded, "errors": list(self.errors)}
