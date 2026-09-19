"""The Lab worker runs one trial in a fresh subprocess and prints JSON events."""

import json
import subprocess
import sys


def run_worker(*args):
    proc = subprocess.run([sys.executable, "-m", "vayunx_lab.worker", *args], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
    return events


def test_worker_fast_preset_reports_histogram_cpu_and_memory():
    events = run_worker("--preset", "md5", "--duration", "0.3", "--warmup", "0.05")
    kinds = [e["event"] for e in events]
    assert kinds[0] == "ready" and "measure_start" in kinds and "measure_end" in kinds and kinds[-1] == "result"
    r = events[-1]
    assert r["preset"] == "md5" and r["ops"] > 100
    assert r["histogram"]["count"] == r["ops"] and r["histogram"]["scheme"] == "log2x8-ns"
    assert r["wall_s"] > 0.25 and r["ops_per_s"] > 0
    assert r["cpu_user_s"] + r["cpu_system_s"] > 0 and r["cpu_s_per_op"] > 0
    assert r["peak_rss_bytes"] >= r["rss_before_bytes"] > 0
    assert r["peak_rss_method"] in ("psutil.peak_wset", "resource.ru_maxrss")
    assert r["threads_max"] >= 1 and r["concurrency"] == 1
    assert r["env"]["python"] and r["env"]["cpu_count_logical"] >= 1
    assert r["p50_ns"] <= r["p95_ns"] <= r["p99_ns"]


def test_worker_slow_preset_respects_min_ops():
    r = run_worker("--preset", "argon2id-owasp", "--duration", "0.05", "--min-ops", "3", "--warmup", "0")[-1]
    assert r["ops"] >= 3 and r["histogram"]["count"] == r["ops"]
    # few ops -> raw durations kept -> exact percentiles, within the observed range
    assert r["percentile_method"] == "exact" and r["p50_ns"] == r["exact_percentiles"]["p50_ns"]
    assert r["histogram"]["min_ns"] <= r["p50_ns"] <= r["histogram"]["max_ns"]


def test_exact_percentiles_nearest_rank():
    from vayunx_lab.worker import exact_percentiles
    p = exact_percentiles(list(range(1, 101)))
    assert (p["p25_ns"], p["p50_ns"], p["p75_ns"], p["p95_ns"], p["p99_ns"]) == (25, 50, 75, 95, 99)
    assert exact_percentiles([]) is None


def test_worker_rejects_unknown_preset():
    proc = subprocess.run([sys.executable, "-m", "vayunx_lab.worker", "--preset", "nope", "--duration", "0.1"],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0 and "unknown preset" in (proc.stdout + proc.stderr).lower()


def test_worker_concurrency_runs_parallel_workers():
    r = run_worker("--preset", "sha256", "--duration", "0.3", "--warmup", "0", "--concurrency", "2")[-1]
    assert r["concurrency"] == 2 and r["ops"] > 0 and r["threads_max"] >= 2
