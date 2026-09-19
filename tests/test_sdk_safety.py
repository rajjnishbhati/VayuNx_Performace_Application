"""SDK safety contract (spec A). No server: fake transports from conftest.py."""

import threading
import time

import psutil
import pytest

from conftest import DownTransport, FlakyTransport, HangingTransport, RecordingTransport, RejectingTransport
from vayunx_profiler_sdk import ProfilerClient
from vayunx_profiler_sdk.sender import SENDER_THREAD_NAME


def client(transport, **opts):
    return ProfilerClient("http://unused", "safety-test", transport=transport, **opts)


# ---------------------------------------------------------------- the three fact-3 failure cases


def test_a_run_start_with_service_down_does_not_raise(fast_opts):
    c = client(DownTransport(), **fast_opts)
    with c.run(label="x", phase="baseline") as run:
        with run.span("work"):
            pass
    s = c.stats()
    assert s["offline"] is True and s["spans_recorded"] == 1 and s["spans_sent"] == 0


def test_b_more_than_a_batch_of_spans_with_service_down_does_not_raise(fast_opts):
    c = client(DownTransport(), batch_size=500, **fast_opts)
    with c.run(label="x", phase="baseline") as run:
        for _ in range(1200):
            with run.span("work"):
                pass
    assert c.stats()["spans_recorded"] == 1200


def test_c_host_exception_is_never_masked(fast_opts):
    c = client(DownTransport(), **fast_opts)
    original = ValueError("the app's own error")
    with pytest.raises(ValueError) as exc:
        with c.run(label="x", phase="baseline") as run:
            with run.span("work"):
                raise original
    assert exc.value is original


# ---------------------------------------------------------------- threading and queues


def test_no_network_io_on_the_caller_thread(fast_opts):
    t = RecordingTransport()
    c = client(t, **fast_opts)
    with c.run(label="x", phase="baseline") as run:
        c.start_sampling(interval_ms=10, metrics=["cpu_pct", "memory_mb"])
        with run.span("outer"):
            with run.span("inner"):
                time.sleep(0.03)
        op = run.op("md5_hash")
        for _ in range(10):
            with op:
                pass
        c.stop_sampling()
    assert t.calls, "nothing was sent"
    assert {name for name, *_ in t.calls} == {SENDER_THREAD_NAME}


def test_overflow_drops_and_counts(fast_opts):
    c = client(DownTransport(), max_queue=100, **fast_opts)
    with c.run(label="x", phase="baseline") as run:
        for _ in range(250):
            with run.span("work"):
                pass
    s = c.stats()
    assert s["spans_recorded"] == 250 and s["dropped_spans"] == 150


def test_run_exit_flush_has_a_hard_timeout():
    c = client(HangingTransport(delay=5.0), flush_timeout_s=0.3, flush_interval_s=0.05)
    started = time.monotonic()
    with c.run(label="x", phase="baseline") as run:
        with run.span("work"):
            pass
    assert time.monotonic() - started < 1.5
    # the first attempt is still hanging: that must read as offline, not online
    assert c.stats()["offline"] is True
    # process-exit flush must not wait a second time for data that just timed out
    started = time.monotonic()
    c._sender._at_exit()
    assert time.monotonic() - started < 0.1


def test_offline_then_recovers_and_delivers_in_order(fast_opts):
    t = FlakyTransport(failures=3)
    c = client(t, **{**fast_opts, "flush_timeout_s": 3.0})
    with c.run(label="x", phase="baseline") as run:
        for i in range(20):
            with run.span(f"s{i}"):
                pass
    assert t.delivered[0] == "/v1/runs"
    assert t.delivered[-1] == f"/v1/runs/{run.run_id}/complete"
    assert set(t.delivered[1:-1]) == {"/v1/spans"}
    assert len(t.items("/v1/spans")) == 20 and run.spans_sent == 20
    assert c.stats()["offline"] is False


def test_permanent_rejection_is_counted_and_run_still_completes(fast_opts):
    t = RejectingTransport()
    c = client(t, **fast_opts)
    with c.run(label="x", phase="baseline") as run:
        with run.span("work"):
            pass
    s = c.stats()
    assert s["send_errors"] >= 1 and s["dropped_spans"] == 1
    assert f"/v1/runs/{run.run_id}/complete" in t.paths()


def test_counters_are_thread_safe(fast_opts):
    t = RecordingTransport()
    c = client(t, **fast_opts)
    with c.run(label="x", phase="baseline") as run:
        def worker():
            for _ in range(500):
                with run.span("threaded"):
                    pass
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
    s = c.stats()
    assert s["spans_recorded"] == 4000 and s["spans_sent"] == 4000 and s["dropped_spans"] == 0
    assert len(t.items("/v1/spans")) == 4000


def test_internal_errors_are_swallowed_and_counted(fast_opts, monkeypatch):
    c = client(RecordingTransport(), **fast_opts)

    def boom(*a, **k):
        raise RuntimeError("internal bug (test)")

    with c.run(label="x", phase="baseline") as run:
        monkeypatch.setattr(c._sender, "enqueue", boom)
        with run.span("work"):
            pass
        monkeypatch.undo()
    assert c.stats()["internal_errors"] >= 1


def test_sampler_errors_never_raise(fast_opts, monkeypatch):
    c = client(RecordingTransport(), **fast_opts)

    def broken(self):
        raise psutil.AccessDenied(pid=0)

    monkeypatch.setattr(psutil.Process, "memory_info", broken)
    with c.run(label="x", phase="baseline"):
        c.start_sampling(interval_ms=10, metrics=["memory_mb"])
        time.sleep(0.05)
        result = c.stop_sampling()
    assert result["errors"]


def test_close_is_idempotent_and_drains(fast_opts):
    t = RecordingTransport()
    c = client(t, **fast_opts)
    with c.run(label="x", phase="baseline") as run:
        with run.span("work"):
            pass
    assert c.close() is True and c.close() is True
    assert c.stats()["queue_depth"] == 0


def test_misuse_still_raises_clearly():
    c = client(RecordingTransport())
    with pytest.raises(ValueError):
        c.run(label="x", phase="pre")
    with pytest.raises(RuntimeError):
        c.start_sampling()
