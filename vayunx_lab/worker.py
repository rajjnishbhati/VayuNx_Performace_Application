"""One Lab trial, in a fresh subprocess:  python -m vayunx_lab.worker --preset md5 --duration 10

Protocol: warm up, then run the preset's operation until `duration` seconds have passed AND at
least `min_ops` operations are done. Prints one JSON object per line:
    {"event": "ready", ...}  {"event": "measure_start", ...}  {"event": "measure_end", ...}  {"event": "result", ...}

Recorded: per-operation latency histogram, ops/s, CPU time per op (user + system of this process),
RSS before the first operation, peak RSS of the process, max thread count, context switches.
Peak RSS: psutil peak_wset on Windows; resource.getrusage ru_maxrss elsewhere (KiB on Linux, bytes on macOS).
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime, timezone

import psutil

from vayunx_lab.env import fingerprint
from vayunx_lab.presets import PresetError, get_preset
from vayunx_profiler_sdk.histogram import LatencyHistogram

ALLOWED_CONCURRENCY = (1, 2, 4, 8)
MAX_DURATION_S = 120.0


def emit(obj: dict) -> None:
    print(json.dumps(obj), flush=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def peak_rss_bytes_from_rusage(raw: int, platform: str) -> int:
    return raw * 1024 if platform.startswith("linux") else raw


def read_peak_rss(proc: psutil.Process) -> tuple[int, str]:
    if sys.platform == "win32":
        return int(proc.memory_info().peak_wset), "psutil.peak_wset"
    import resource
    return peak_rss_bytes_from_rusage(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss, sys.platform), "resource.ru_maxrss"


def timer_overhead_ns(iterations: int = 20_000) -> float:
    """Median cost the timing loop adds to every measured operation (two clock reads + a call)."""
    perf, noop, h = time.perf_counter_ns, (lambda: None), LatencyHistogram()
    for _ in range(iterations):
        t0 = perf()
        noop()
        h.record(perf() - t0)
    return float(h.percentile(50))


def _measure(op, duration_s: float, min_ops: int, concurrency: int, proc: psutil.Process) -> tuple[LatencyHistogram, int]:
    perf = time.perf_counter_ns
    deadline = perf() + int(duration_s * 1e9)
    if concurrency == 1:
        hist = LatencyHistogram()
        while perf() < deadline or hist.count < min_ops:
            t0 = perf()
            op()
            hist.record(perf() - t0)
        return hist, proc.num_threads()

    hists = [LatencyHistogram() for _ in range(concurrency)]
    done = [0]
    lock = threading.Lock()

    def worker(h: LatencyHistogram):
        while True:
            with lock:
                if perf() >= deadline and done[0] >= min_ops:
                    return
                done[0] += 1  # claim one operation
            t0 = perf()
            op()
            h.record(perf() - t0)

    threads = [threading.Thread(target=worker, args=(h,), name=f"lab-worker-{i}") for i, h in enumerate(hists)]
    for t in threads:
        t.start()
    threads_max = proc.num_threads()
    while any(t.is_alive() for t in threads):
        threads_max = max(threads_max, proc.num_threads())
        time.sleep(0.05)
    for t in threads:
        t.join()
    total = LatencyHistogram()
    for h in hists:
        total.merge(h)
    return total, threads_max


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--preset", required=True)
    p.add_argument("--duration", type=float, default=10.0)
    p.add_argument("--min-ops", type=int, default=None)
    p.add_argument("--warmup", type=float, default=1.0, help="seconds of warm-up operations (not measured)")
    p.add_argument("--concurrency", type=int, default=1, choices=ALLOWED_CONCURRENCY)
    args = p.parse_args(argv)
    try:
        preset = get_preset(args.preset)
    except PresetError as exc:
        emit({"event": "error", "error": str(exc)})
        return 2
    if not 0 < args.duration <= MAX_DURATION_S:
        emit({"event": "error", "error": f"duration must be in (0, {MAX_DURATION_S}] seconds"})
        return 2
    min_ops = preset.min_ops if args.min_ops is None else max(0, args.min_ops)

    proc = psutil.Process()
    op = preset.make_op()
    overhead = timer_overhead_ns()
    rss_before = proc.memory_info().rss
    emit({"event": "ready", "pid": proc.pid, "preset": preset.id, "ts": now_iso()})

    warm_deadline = time.perf_counter() + args.warmup
    while time.perf_counter() < warm_deadline:
        op()

    cpu0, ctx0 = proc.cpu_times(), proc.num_ctx_switches()
    start_iso, t0 = now_iso(), time.perf_counter()
    emit({"event": "measure_start", "ts": start_iso})
    hist, threads_max = _measure(op, args.duration, min_ops, args.concurrency, proc)
    wall = time.perf_counter() - t0
    end_iso = now_iso()
    cpu1, ctx1 = proc.cpu_times(), proc.num_ctx_switches()
    emit({"event": "measure_end", "ts": end_iso})

    peak, method = read_peak_rss(proc)
    cpu_user, cpu_sys = cpu1.user - cpu0.user, cpu1.system - cpu0.system
    ops = hist.count
    emit({
        "event": "result", "preset": preset.id, "variant": preset.label, "algorithm": preset.algorithm,
        "params": preset.params, "library": preset.library_version(), "concurrency": args.concurrency,
        "duration_target_s": args.duration, "min_ops": min_ops, "warmup_s": args.warmup,
        "ops": ops, "wall_s": wall, "ops_per_s": ops / wall if wall else None,
        "cpu_user_s": cpu_user, "cpu_system_s": cpu_sys, "cpu_s_per_op": (cpu_user + cpu_sys) / ops if ops else None,
        "cores_busy": (cpu_user + cpu_sys) / wall if wall else None,
        "rss_before_bytes": rss_before, "peak_rss_bytes": peak, "peak_rss_method": method,
        "threads_max": threads_max,
        "ctx_switches": (ctx1.voluntary - ctx0.voluntary) + (ctx1.involuntary - ctx0.involuntary),
        "histogram": hist.to_dict(), "mean_ns": hist.sum_ns / ops if ops else None,
        "p50_ns": hist.percentile(50), "p95_ns": hist.percentile(95), "p99_ns": hist.percentile(99),
        "timer_overhead_ns": overhead,
        "measure_start": start_iso, "measure_end": end_iso, "env": fingerprint(),
    })
    return 0


if __name__ == "__main__":
    sys.exit(main())
