"""Lab experiment runner.

Protocol (spec B):
* Every (variant, trial) runs in a FRESH worker subprocess with its own warm-up.
* Variants are interleaved across trials with alternating order (A B, B A, A B, ...), so slow drift
  (thermal throttling, background jobs) spreads across variants instead of landing on one.
* Before each trial, a quiet-machine check waits (bounded) for machine CPU to settle below a
  threshold; if it never does, the trial still runs but is flagged noisy.
* While the worker runs, this process samples the worker and the machine every sample_interval_s
  (the machine lens). The sampler lives here, not in the worker, so it doesn't perturb the worker's
  own measurements; machine CPU therefore includes this sampler (small; measured and stored).
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Callable

import psutil

from vayunx_lab.env import fingerprint
from vayunx_lab.node_runtime import node_executable, worker_path
from vayunx_lab.presets import get_preset
from vayunx_lab.worker import ALLOWED_CONCURRENCY, MAX_DURATION_S
from vayunx_profiler_sdk.metrics import UNITS, MachineReader, ProcessReader

MAX_TRIALS = 20
NOISY_OTHER_CORES = 0.5  # other work using more than half a core during a trial = noisy


class TrialError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


def machine_busy_cpu_s() -> float:
    """Cumulative CPU-seconds the whole machine spent not idle (all logical CPUs)."""
    t = psutil.cpu_times()
    idle = t.idle + getattr(t, "iowait", 0.0)
    return sum(t) - idle


def other_cores_from_counters(busy_start_s: float | None, busy_end_s: float | None, proc_cpu_s: float | None,
                              wall_s: float | None) -> float | None:
    """Cores used by work OTHER than the benchmark process during a trial, from cumulative counters:
    (machine busy CPU-seconds - benchmark CPU-seconds) / wall seconds. Unlike subtracting two instantaneous
    CPU percentages (sampled over separate 100 ms windows), this does not drift with the benchmark's own load."""
    if None in (busy_start_s, busy_end_s, proc_cpu_s, wall_s) or not wall_s or wall_s <= 0:
        return None
    return max(0.0, (busy_end_s - busy_start_s - proc_cpu_s) / wall_s)


def schedule(preset_ids: list[str], trials: int) -> list[tuple[int, str]]:
    out = []
    for t in range(trials):
        order = preset_ids if t % 2 == 0 else list(reversed(preset_ids))
        out.extend((t, p) for p in order)
    return out


def quiet_check(read_cpu: Callable[[], float] = lambda: psutil.cpu_percent(interval=0.5), threshold_pct: float = 25.0,
                max_wait_s: float = 5.0, sleep: Callable[[float], None] = time.sleep, step_s: float = 0.5) -> dict:
    waited = 0.0
    reading = read_cpu()
    while reading > threshold_pct and waited < max_wait_s:
        sleep(step_s)
        waited += step_s
        reading = read_cpu()
    return {"quiet": reading <= threshold_pct, "machine_cpu_pct": reading, "waited_s": waited,
            "threshold_pct": threshold_pct, "max_wait_s": max_wait_s}


class _TrialSampler(threading.Thread):
    def __init__(self, pid: int, interval_s: float):
        super().__init__(name="vayunx-lab-sampler", daemon=True)
        self.pid, self.interval_s = pid, interval_s
        self.stop_event = threading.Event()
        self.rows: list[dict] = []
        self.other: list[tuple[datetime, float]] = []
        self.overheads_ms: list[float] = []

    def run(self) -> None:
        try:
            proc_reader, machine = ProcessReader(self.pid), MachineReader()
            proc_reader.prime()
            machine.prime()
        except psutil.Error:
            return
        last_full = 0.0
        while not self.stop_event.wait(self.interval_s):
            t0 = time.perf_counter()
            ts = datetime.now(timezone.utc)
            try:
                p = proc_reader.read()
            except psutil.Error:
                break
            full = t0 - last_full >= 1.0
            if full:
                last_full = t0
            m = machine.read(full=full)
            other = machine.other_cores_busy(m, p)
            for name, value in {**p, **m, "machine_other_cores_busy": other}.items():
                self.rows.append({"metric_name": name, "value": float(value), "unit": UNITS[name], "timestamp": ts})
            self.other.append((ts, other))
            self.overheads_ms.append((time.perf_counter() - t0) * 1000)


class ExperimentRunner:
    def __init__(self, store, preset_ids: list[str], *, trials: int = 5, duration_s: float = 10.0, concurrency: int = 1,
                 reference: str | None = None, warmup_s: float = 1.0, sample_interval_s: float = 0.1,
                 quiet_threshold_pct: float = 25.0, quiet_max_wait_s: float = 5.0, python: str = sys.executable,
                 cancel_event: threading.Event | None = None, on_trial_done: Callable[[int, int], None] | None = None,
                 log: Callable[[str], None] | None = None, label: str | None = None):
        if len(preset_ids) < 1 or len(set(preset_ids)) != len(preset_ids):
            raise ValueError("choose one or more distinct presets")
        self.presets = [get_preset(p) for p in preset_ids]  # validates against the allow-list
        if not 1 <= trials <= MAX_TRIALS:
            raise ValueError(f"trials must be 1-{MAX_TRIALS}")
        if not 0 < duration_s <= MAX_DURATION_S:
            raise ValueError(f"duration_s must be in (0, {MAX_DURATION_S}]")
        if concurrency not in ALLOWED_CONCURRENCY:
            raise ValueError(f"concurrency must be one of {ALLOWED_CONCURRENCY}")
        if concurrency != 1 and any(p.runtime == "node" for p in self.presets):
            raise ValueError("the Node.js runner supports concurrency 1 only; run Node.js variants with concurrency 1")
        reference = reference or preset_ids[0]
        if reference not in preset_ids:
            raise ValueError("reference must be one of the chosen presets")
        self.preset_ids, self.trials, self.duration_s, self.concurrency = preset_ids, trials, duration_s, concurrency
        self.reference, self.warmup_s, self.sample_interval_s = reference, warmup_s, sample_interval_s
        self.quiet_threshold_pct, self.quiet_max_wait_s, self.python = quiet_threshold_pct, quiet_max_wait_s, python
        self.cancel_event = cancel_event or threading.Event()
        self.on_trial_done, self.log = on_trial_done, log or (lambda msg: None)
        self.label = label or " vs ".join(p.label for p in self.presets)
        self.store = store

    def create(self) -> str:
        return self.store.create_experiment(self.preset_ids, self.trials, self.duration_s, self.concurrency,
                                            self.reference, self.label, env=fingerprint())

    def run(self, exp_id: str) -> str:
        plan = schedule(self.preset_ids, self.trials)
        self.store.set_status(exp_id, "running")
        try:
            for i, (trial, preset_id) in enumerate(plan):
                if self.cancel_event.is_set() or self.store.status(exp_id) == "cancelling":
                    raise Cancelled()
                preset = get_preset(preset_id)
                current = {"trial": trial + 1, "trials": self.trials, "preset": preset_id, "variant": preset.label}
                self.store.update_progress(exp_id, i, len(plan), {**current, "stage": "quiet-check"})
                quiet = quiet_check(threshold_pct=self.quiet_threshold_pct, max_wait_s=self.quiet_max_wait_s)
                self.store.update_progress(exp_id, i, len(plan), {**current, "stage": "measuring",
                                                                   "started_at": datetime.now(timezone.utc).isoformat()})
                self.log(f"[{i + 1}/{len(plan)}] trial {trial + 1} · {preset.label}")
                result, sampler, busy = self._run_trial(preset_id)
                start, end = datetime.fromisoformat(result["measure_start"]), datetime.fromisoformat(result["measure_end"])
                during = [v for ts, v in sampler.other if start <= ts <= end]
                other_median = statistics.median(during) if during else None  # time series view only
                other_trial = other_cores_from_counters(busy.get("start"), busy.get("end"),
                                                        result["cpu_user_s"] + result["cpu_system_s"], result["wall_s"])
                noise = other_trial if other_trial is not None else other_median
                noisy = (not quiet["quiet"]) or (noise is not None and noise > NOISY_OTHER_CORES)
                self.store.save_trial(exp_id=exp_id, preset=preset, trial_index=trial,
                                      phase="baseline" if preset_id == self.reference else "remediated",
                                      result=result, samples=sampler.rows, quiet=quiet, noisy=noisy,
                                      other_cores_busy_median=other_median, other_cores_busy_trial=other_trial,
                                      sampler_overhead_ms=statistics.mean(sampler.overheads_ms) if sampler.overheads_ms else None)
                self.store.update_progress(exp_id, i + 1, len(plan), {**current, "stage": "done"})
                if self.on_trial_done:
                    self.on_trial_done(i + 1, len(plan))
            if self.cancel_event.is_set():
                raise Cancelled()
            self.store.set_status(exp_id, "complete")
            return "complete"
        except Cancelled:
            self.store.set_status(exp_id, "cancelled")
            return "cancelled"
        except TrialError as exc:
            self.store.set_status(exp_id, "failed", error=str(exc))
            return "failed"
        except Exception as exc:  # never leave an experiment stuck in "running"
            self.store.set_status(exp_id, "failed", error=f"unexpected error: {exc!r}")
            return "failed"

    def _command(self, preset_id: str) -> list[str]:
        preset = get_preset(preset_id)  # allow-list check again, right before launching anything
        args = ["--preset", preset.base_id, "--duration", str(self.duration_s), "--warmup", str(self.warmup_s),
                "--concurrency", str(self.concurrency)]
        if preset.runtime == "node":
            node = node_executable()
            if node is None:
                raise TrialError("Node.js was not found; install it or set VAYUNX_NODE to run Node.js variants")
            return [node, str(worker_path()), *args]
        return [self.python, "-m", "vayunx_lab.worker", *args]

    def _run_trial(self, preset_id: str) -> tuple[dict, "_TrialSampler", dict]:
        cmd = self._command(preset_id)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        except OSError as exc:
            raise TrialError(f"could not start the worker process ({exc})") from None
        sampler: _TrialSampler | None = None
        watcher_stop = threading.Event()

        def watch_cancel():  # a cancel request stops the current trial too, not just the next one
            while not watcher_stop.wait(0.2):
                if self.cancel_event.is_set() and proc.poll() is None:
                    proc.terminate()
                    return

        threading.Thread(target=watch_cancel, daemon=True).start()
        events = []
        busy: dict = {}  # machine busy CPU-seconds at the worker's measure_start / measure_end
        try:
            for line in proc.stdout:
                if not line.strip():
                    continue
                event = json.loads(line)
                events.append(event)
                if event["event"] == "measure_start":
                    busy["start"] = machine_busy_cpu_s()
                    if sampler is not None:  # read from outside, for workers that cannot count these themselves
                        try:
                            wp = psutil.Process(sampler.pid)
                            busy["threads"], busy["ctx_start"] = wp.num_threads(), sum(wp.num_ctx_switches()[:2])
                        except psutil.Error:
                            pass
                elif event["event"] == "measure_end":
                    busy["end"] = machine_busy_cpu_s()
                    if sampler is not None and "ctx_start" in busy:
                        try:
                            busy["ctx_end"] = sum(psutil.Process(sampler.pid).num_ctx_switches()[:2])
                        except psutil.Error:
                            pass
                if event["event"] == "ready" and sampler is None:
                    sampler = _TrialSampler(event["pid"], self.sample_interval_s)
                    sampler.start()
                elif event["event"] == "error":
                    raise TrialError(event["error"])
            proc.wait()
        finally:
            watcher_stop.set()
            if sampler:
                sampler.stop_event.set()
                sampler.join(timeout=5)
            if proc.poll() is None:
                proc.kill()
        if self.cancel_event.is_set():
            raise Cancelled()
        if proc.returncode != 0 or not events or events[-1]["event"] != "result":
            raise TrialError(f"worker for {preset_id} failed (exit {proc.returncode}): {proc.stderr.read()[-500:]}")
        result = events[-1]
        sampler = sampler or _TrialSampler(0, self.sample_interval_s)
        if result.get("threads_max") is None:  # the Node.js worker: thread count as seen by this process's sampler
            seen = [r["value"] for r in sampler.rows if r["metric_name"] == "proc_threads"]
            if "threads" in busy:
                seen.append(busy["threads"])
            if not seen:
                raise TrialError(f"worker for {preset_id} reported no thread count and none could be sampled")
            result["threads_max"] = int(max(seen))
            result["threads_max_method"] = "sampled by the runner (psutil)"
        if result.get("ctx_switches") is None:  # libuv reports none on Windows: count them from outside instead
            if "ctx_end" not in busy:
                raise TrialError(f"worker for {preset_id} reported no context switches and none could be read")
            result["ctx_switches"] = busy["ctx_end"] - busy["ctx_start"]
            result["ctx_switches_method"] = "psutil, read by the runner at measure_start / measure_end"
        return result, sampler, busy
