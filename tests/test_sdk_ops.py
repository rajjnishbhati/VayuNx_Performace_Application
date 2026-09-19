"""Fast-path operations: in-process latency histograms, sent as one summary per interval."""

import hashlib
import time

from conftest import RecordingTransport
from vayunx_profiler_sdk import ProfilerClient


def make(fast_opts, **extra):
    t = RecordingTransport()
    return t, ProfilerClient("http://unused", "ops-test", transport=t, **{**fast_opts, **extra})


def test_op_aggregates_many_calls_into_summaries(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline") as run:
        md5 = run.op("md5_hash", category="cryptographic", attributes={"crypto.algorithm": "MD5"})
        for _ in range(5000):
            with md5:
                hashlib.md5(b"synthetic password").digest()
    stats = t.items("/v1/op-stats")
    assert stats, "no op summaries sent"
    assert sum(s["count"] for s in stats) == 5000
    s = stats[0]
    assert s["op_name"] == "md5_hash" and s["category"] == "cryptographic" and s["attributes"] == {"crypto.algorithm": "MD5"}
    assert s["histogram"]["scheme"] and s["p50_ns"] <= s["p95_ns"] <= s["p99_ns"] <= s["max_ns"]
    assert s["sdk_overhead_ns"] is not None and s["sdk_overhead_ns"] > 0
    assert run.op_stats_sent == len(stats)
    assert not t.items("/v1/spans"), "fast calls must not become spans by default"


def test_slow_calls_also_become_spans(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline") as run:
        with run.span("login"):
            op = run.op("slow_kdf", slow_ms=5.0)
            with op:
                time.sleep(0.02)
            with op:
                pass
    spans = t.items("/v1/spans")
    slow = [s for s in spans if s["span_name"] == "slow_kdf"]
    login = [s for s in spans if s["span_name"] == "login"][0]
    assert len(slow) == 1 and slow[0]["parent_span_id"] == login["span_id"] and slow[0]["attributes"].get("vayunx.sampled") == "slow"
    assert sum(s["count"] for s in t.items("/v1/op-stats")) == 2


def test_every_nth_call_is_kept_as_a_span(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline") as run:
        op = run.op("hmac", span_sample_every=100, slow_ms=None)
        for _ in range(1000):
            with op:
                pass
    assert len([s for s in t.items("/v1/spans") if s["span_name"] == "hmac"]) == 10


def test_wrap_decorator_counts_calls(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline") as run:
        @run.op("sha256").wrap
        def digest(data):
            return hashlib.sha256(data).digest()
        for _ in range(100):
            digest(b"abc")
    assert sum(s["count"] for s in t.items("/v1/op-stats")) == 100


def test_op_overhead_is_measured_and_reported(fast_opts):
    t, c = make(fast_opts)
    with c.run(label="x", phase="baseline") as run:
        run.op("noop")
        overhead = c.stats()["op_overhead_ns"]
    assert overhead is not None and 0 < overhead < 50_000
