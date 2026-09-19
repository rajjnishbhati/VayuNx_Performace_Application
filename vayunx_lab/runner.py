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
from vayunx_lab.presets import get_preset
from vayunx_lab.worker import ALLOWED_CONCURRENCY, MAX_DURATION_S
from vayunx_profiler_sdk.metrics import UNITS, MachineReader, ProcessReader

MAX_TRIALS = 20
NOISY_OTHER_CORES = 0.5  # other work using more than half a core during a trial = noisy


class TrialError(RuntimeError):
    pass


class Cancelled(RuntimeError):
    pass


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
                result, sampler = self._run_trial(preset_id)
                start, end = datetime.fromisoformat(result["measure_start"]), datetime.fromisoformat(result["measure_end"])
                during = [v for ts, v in sampler.other if start <= ts <= end]
                other_median = statistics.median(during) if during else None
                noisy = (not quiet["quiet"]) or (other_median is not None and other_median > NOISY_OTHER_CORES)
                self.store.save_trial(exp_id=exp_id, preset=preset, trial_index=trial,
                                      phase="baseline" if preset_id == self.reference else "remediated",
                                      result=result, samples=sampler.rows, quiet=quiet, noisy=noisy,
                                      other_cores_busy_median=other_median,
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

    def _run_trial(self, preset_id: str) -> tuple[dict, _TrialSampler]:
        cmd = [self.python, "-m", "vayunx_lab.worker", "--preset", preset_id, "--duration", str(self.duration_s),
               "--warmup", str(self.warmup_s), "--concurrency", str(self.concurrency)]
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
        try:
            for line in proc.stdout:
                if not line.strip():
                    continue
                event = json.loads(line)
                events.append(event)
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
        return events[-1], sampler or _TrialSampler(0, self.sample_interval_s)
