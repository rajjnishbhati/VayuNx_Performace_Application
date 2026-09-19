"""Fake transports for SDK tests - no server needed - and the database fixture shared by service tests.

`db_url(tmp_path)` returns a fresh, empty database: SQLite by default, PostgreSQL with VAYUNX_TEST_DB=postgres
(see tests/pgtools.py for where the server comes from)."""

import os
import threading
import time
import uuid

import pytest

from vayunx_profiler_sdk import ProfilerConnectionError, ProfilerHTTPError


class RecordingTransport:
    """Accepts everything; records (thread name, method, path, payload) for every call."""

    def __init__(self):
        self.calls = []
        self._lock = threading.Lock()

    def _record(self, method, path, payload):
        with self._lock:
            self.calls.append((threading.current_thread().name, method, path, payload))

    def post(self, path, payload):
        self._record("POST", path, payload)
        if path == "/v1/runs":
            return {"run_id": payload.get("run_id", "generated")}
        if path.endswith("/complete"):
            return {"run_id": path.split("/")[3]}
        return {"accepted": len(payload) if isinstance(payload, list) else 1}

    def get(self, path):
        self._record("GET", path, None)
        return {}

    def paths(self):
        return [c[2] for c in self.calls]

    def items(self, path):
        out = []
        for _, _, p, payload in self.calls:
            if p == path:
                out.extend(payload if isinstance(payload, list) else [payload])
        return out


class DownTransport(RecordingTransport):
    """Service unreachable for every call."""

    def post(self, path, payload):
        self._record("POST", path, payload)
        raise ProfilerConnectionError("service down (test)")


class HangingTransport(RecordingTransport):
    """Every call blocks for `delay` seconds, then fails."""

    def __init__(self, delay=5.0):
        super().__init__()
        self.delay = delay

    def post(self, path, payload):
        self._record("POST", path, payload)
        time.sleep(self.delay)
        raise ProfilerConnectionError("timed out (test)")


class FlakyTransport(RecordingTransport):
    """Fails the first `failures` calls with a connection error, then behaves like RecordingTransport."""

    def __init__(self, failures):
        super().__init__()
        self.failures = failures
        self.delivered = []

    def post(self, path, payload):
        if self.failures > 0:
            self.failures -= 1
            self._record("POST-FAILED", path, payload)
            raise ProfilerConnectionError("service down (test)")
        result = super().post(path, payload)
        self.delivered.append(path)
        return result


class RejectingTransport(RecordingTransport):
    """Permanently rejects span batches with HTTP 422; accepts everything else."""

    def post(self, path, payload):
        if path == "/v1/spans":
            self._record("POST-REJECTED", path, payload)
            raise ProfilerHTTPError(422, "bad span (test)", "POST", path)
        return super().post(path, payload)


FAST = dict(flush_interval_s=0.05, flush_timeout_s=0.5, backoff_initial_s=0.01, backoff_max_s=0.05)


@pytest.fixture
def fast_opts():
    return dict(FAST)


@pytest.fixture(autouse=True)
def _shutdown_clients(monkeypatch):
    """Stop every ProfilerClient a test creates, so offline senders don't keep retrying in the
    background and starve later (timing-sensitive) tests of the GIL."""
    from vayunx_profiler_sdk import ProfilerClient

    created = []
    original = ProfilerClient.__init__

    def tracking_init(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(ProfilerClient, "__init__", tracking_init)
    yield
    for c in created:
        c.shutdown(timeout=0)


@pytest.fixture(scope="session")
def _pg_server():
    if os.environ.get("VAYUNX_TEST_DB") != "postgres":
        yield None
        return
    from pgtools import PgServer
    server = PgServer()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def db_url(_pg_server):
    """Call db_url(tmp_dir) for a new empty database URL."""
    def make(tmp_dir) -> str:
        if _pg_server is not None:
            return _pg_server.new_database()
        return f"sqlite:///{tmp_dir}/{uuid.uuid4().hex[:8]}.db"
    return make
