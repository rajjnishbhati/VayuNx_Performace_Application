"""Sampler v2 (spec C): process cost + machine noise, adaptive interval, crypto-aligned tagging."""

import time
from datetime import datetime

from conftest import RecordingTransport
from vayunx_profiler_sdk import ProfilerClient


def make(fast_opts):
    t = RecordingTransport()
    return t, ProfilerClient("http://unused", "sampler-v2", transport=t, **fast_opts)


def ts(s):
    return datetime.fromisoformat(s["timestamp"])


def test_default_metrics_cover_process_cost_and_machine_noise(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline"):
        c.start_sampling()
        time.sleep(1.3)
        c.stop_sampling()
    names = {s["metric_name"] for s in t.items("/v1/samples")}
    assert {"proc_cores_busy", "proc_cpu_time_s", "proc_rss_mib", "proc_threads", "proc_ctx_switches"} <= names
    assert {"machine_cpu_pct", "machine_mem_available_mib", "machine_other_cores_busy"} <= names


def test_adaptive_interval_is_fast_during_crypto_and_slow_when_idle(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline") as run:
        c.start_sampling()
        time.sleep(1.2)  # idle: ~1 s interval
        with run.span("hash_password", category="cryptographic") as span:
            time.sleep(0.4)  # crypto open: ~10 ms interval
        time.sleep(0.2)
        c.stop_sampling()
    proc = [s for s in t.items("/v1/samples") if s["metric_name"] == "proc_cores_busy"]
    start = span._start_wall
    end = start.timestamp() + span.duration_ms / 1000
    inside = [s for s in proc if start.timestamp() <= ts(s).timestamp() <= end]
    before = [s for s in proc if ts(s).timestamp() < start.timestamp()]
    # Fast mode targets 10 ms; on Windows the default timer granularity (~15.6 ms) and GIL hand-offs stretch
    # that, so assert "clearly fast" (idle would give 0-1 samples here) rather than an exact count.
    assert len(inside) >= 5, f"only {len(inside)} samples during a 400 ms crypto span"
    assert len(before) <= 3, f"{len(before)} samples in 1.2 s of idle time"


def test_samples_are_tagged_cryptographic_only_while_crypto_runs(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline") as run:
        c.start_sampling()
        with run.span("hash_password", category="cryptographic"):
            time.sleep(0.3)
        time.sleep(1.2)
        c.stop_sampling()
    by_cat = {}
    for s in t.items("/v1/samples"):
        by_cat.setdefault(s["category"], []).append(s)
    assert by_cat.get("cryptographic") and by_cat.get("general")
    last_crypto = max(ts(s) for s in by_cat["cryptographic"])
    assert any(ts(s) > last_crypto for s in by_cat["general"])


def test_fast_path_ops_also_switch_to_fast_sampling(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline") as run:
        c.start_sampling()
        op = run.op("md5_hash")
        deadline = time.perf_counter() + 0.4
        while time.perf_counter() < deadline:
            with op:
                pass
        c.stop_sampling()
    crypto = [s for s in t.items("/v1/samples") if s["category"] == "cryptographic" and s["metric_name"] == "proc_cores_busy"]
    # A tight Python loop holds the GIL, so the sampler runs less often than its 10 ms target; >= 5 in 400 ms
    # still proves fast mode engaged (the 1 s idle interval would give 0-1).
    assert len(crypto) >= 5


def test_sampler_reports_its_own_overhead(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline"):
        c.start_sampling()
        time.sleep(0.5)
        result = c.stop_sampling()
    assert result["ticks"] >= 1 and 0 < result["overhead_ms_per_tick"] < 50
    assert c.stats()["sampler_overhead_ms_per_tick"] == result["overhead_ms_per_tick"]


def test_v1_metrics_and_fixed_interval_still_work(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline"):
        c.start_sampling(interval_ms=20, metrics=["cpu_pct", "memory_mb"], category="cryptographic")
        time.sleep(0.2)
        c.stop_sampling()
    samples = t.items("/v1/samples")
    assert {s["metric_name"] for s in samples} == {"cpu_pct", "memory_mb"}
    assert {s["category"] for s in samples} == {"cryptographic"}
