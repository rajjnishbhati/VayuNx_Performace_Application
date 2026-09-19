"""Background sender: the only place the SDK does network I/O.

* One bounded FIFO queue per client. Order is preserved, so a run's creation is always sent
  before its spans/samples/op-stats, and its completion after them.
* Data items (span, sample, op_stats) are batched by size or by time. Control items (run,
  complete) are sent one at a time and are never dropped.
* When the queue holds `max_queue` data items, new data is dropped and counted.
* Connection failures, timeouts, 5xx, 408 and 429 put the sender in offline mode: the batch
  stays queued and is retried with exponential backoff. Other 4xx answers are permanent: the
  batch is dropped and counted.
* flush(timeout) waits for the queue to drain, but never longer than `timeout`.
* Nothing here raises into host code; failures are counted and logged once per kind.
"""

from __future__ import annotations

import atexit
import itertools
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime

from vayunx_profiler_sdk.transport import ProfilerConnectionError, ProfilerHTTPError

SENDER_THREAD_NAME = "vayunx-profiler-sender"
DATA_PATHS = {"span": "/v1/spans", "sample": "/v1/samples", "op_stats": "/v1/op-stats"}
PLURAL = {"span": "spans", "sample": "samples", "op_stats": "op_stats"}
_RECORDED = {k: f"{v}_recorded" for k, v in PLURAL.items()}
_DROPPED = {k: f"dropped_{v}" for k, v in PLURAL.items()}
RETRYABLE_STATUS = {408, 429}
log = logging.getLogger("vayunx_profiler")


class BackgroundSender:
    def __init__(self, transport, *, max_queue: int = 10_000, batch_size: int = 500, flush_interval_s: float = 1.0,
                 flush_timeout_s: float = 2.0, backoff_initial_s: float = 0.5, backoff_max_s: float = 30.0, on_tick=None):
        self.transport = transport
        self.max_queue = max_queue
        self.batch_size = max(1, batch_size)
        self.flush_interval_s = flush_interval_s
        self.flush_timeout_s = flush_timeout_s
        self.backoff_initial_s = backoff_initial_s
        self.backoff_max_s = backoff_max_s
        self.on_tick = on_tick

        self._queue: deque = deque()
        self._data_count = 0
        self._cond = threading.Condition()
        self._in_flight = False
        self._flush_now = False
        self._thread: threading.Thread | None = None
        # "offline" = the service has not yet accepted anything, or the last attempt failed.
        # (An attempt still in flight - e.g. a slow connection refusal - must not read as "online".)
        self._ever_reached = False
        self._last_failed = False
        self._flush_timed_out = False
        self._counters: dict[str, int] = defaultdict(int)
        self._per_run: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
        self._warned: set[str] = set()
        atexit.register(self._at_exit)

    # ---------------------------------------------------------------- producer side (any thread)

    def enqueue(self, kind: str, run_id: str, payload) -> bool:
        is_data = kind in DATA_PATHS
        with self._cond:
            if is_data:
                self._counters[_RECORDED[kind]] += 1
                if self._data_count >= self.max_queue:
                    self._counters[_DROPPED[kind]] += 1
                    self._warn_once("overflow", f"profiler queue full ({self.max_queue}); dropping data (see stats())")
                    return False
                self._data_count += 1
            self._queue.append((kind, run_id, payload))
            if self._thread is None:
                self._ensure_thread()
            if not is_data or self._data_count >= self.batch_size:
                self._ensure_thread()  # also restarts the thread if it ever died
                self._cond.notify_all()
        return True

    def flush(self, timeout: float | None = None) -> bool:
        """Wait (bounded) until everything queued has been sent or dropped. True if drained."""
        deadline = time.monotonic() + (self.flush_timeout_s if timeout is None else timeout)
        with self._cond:
            self._flush_now = True
            self._cond.notify_all()
            while self._queue or self._in_flight:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._flush_timed_out = True
                    return False
                self._cond.wait(remaining)
        return True

    def stats(self) -> dict:
        with self._cond:
            out = dict(self._counters)
            out.update(offline=(not self._ever_reached) or self._last_failed, queue_depth=len(self._queue))
        for kind in PLURAL.values():
            for suffix in ("recorded", "sent"):
                out.setdefault(f"{kind}_{suffix}", 0)
            out.setdefault(f"dropped_{kind}", 0)
        out.setdefault("send_errors", 0)
        return out

    def delivered(self, run_id: str, kind: str) -> int:
        with self._cond:
            return self._per_run[run_id][kind]

    # ---------------------------------------------------------------- consumer side (sender thread)

    def _ensure_thread(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._loop, name=SENDER_THREAD_NAME, daemon=True)
            self._thread.start()

    def _take_group(self) -> list:
        if not self._queue:
            return []
        kind, run_id, _ = self._queue[0]
        if kind not in DATA_PATHS:
            return [self._queue[0]]
        group = []
        for item in itertools.islice(self._queue, 0, self.batch_size):
            if item[0] != kind or item[1] != run_id:
                break
            group.append(item)
        return group

    def _loop(self) -> None:
        backoff = self.backoff_initial_s
        last_tick = time.monotonic()
        while True:
            with self._cond:
                if not self._flush_now and self._data_count < self.batch_size and not self._control_waiting():
                    self._cond.wait(self.flush_interval_s)
                self._flush_now = False
            if self.on_tick and time.monotonic() - last_tick >= self.flush_interval_s:
                last_tick = time.monotonic()
                try:
                    self.on_tick()
                except Exception as exc:  # pragma: no cover - defensive
                    self._count_internal("tick", exc)
            while True:
                with self._cond:
                    group = self._take_group()
                    if not group:
                        self._cond.notify_all()
                        break
                    self._in_flight = True
                outcome = self._send(group)
                with self._cond:
                    self._in_flight = False
                    if outcome == "retry":
                        self._last_failed = True
                        self._cond.notify_all()
                    else:
                        for _ in group:
                            self._queue.popleft()
                        kind, run_id, _ = group[0]
                        if kind in DATA_PATHS:
                            self._data_count -= len(group)
                            key = "sent" if outcome == "ok" else "dropped"
                            name = f"{PLURAL[kind]}_sent" if outcome == "ok" else f"dropped_{PLURAL[kind]}"
                            self._counters[name] += len(group)
                            if key == "sent":
                                self._per_run[run_id][PLURAL[kind]] += len(group)
                        # "ok" and permanent rejections both prove the service is reachable
                        self._ever_reached, self._last_failed, self._flush_timed_out = True, False, False
                        backoff = self.backoff_initial_s
                        self._cond.notify_all()
                if outcome == "retry":
                    time.sleep(backoff)
                    backoff = min(backoff * 2, self.backoff_max_s)
                    break

    def _control_waiting(self) -> bool:
        return any(item[0] not in DATA_PATHS for item in self._queue)

    @staticmethod
    def _wire(payload: dict) -> dict:
        """Serialise deferred fields on this (sender) thread, keeping that cost off the host's thread."""
        if any(isinstance(v, datetime) for v in payload.values()):
            return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in payload.items()}
        return payload

    def _send(self, group: list) -> str:
        kind, run_id, payload = group[0]
        try:
            if kind == "run":
                self.transport.post("/v1/runs", payload)
            elif kind == "complete":
                self.transport.post(f"/v1/runs/{run_id}/complete", payload or {})
            else:
                self.transport.post(DATA_PATHS[kind], [self._wire(item[2]) for item in group])
            return "ok"
        except ProfilerHTTPError as exc:
            if exc.status >= 500 or exc.status in RETRYABLE_STATUS:
                self._warn_once(f"http{exc.status}", f"profiler service error {exc.status}; will retry")
                return "retry"
            if exc.status == 409 and kind in ("run", "complete"):
                return "ok"  # already created/completed, e.g. by an earlier attempt that timed out
            with self._cond:
                self._counters["send_errors"] += 1
            self._warn_once(f"reject-{kind}-{exc.status}", f"profiler service rejected {kind} data: {exc}")
            return "dropped"
        except (ProfilerConnectionError, OSError, TimeoutError) as exc:
            self._warn_once("offline", f"profiler service unreachable, buffering and retrying: {exc}")
            return "retry"
        except Exception as exc:
            self._count_internal(f"send-{kind}", exc)
            return "dropped"

    # ---------------------------------------------------------------- helpers

    def _count_internal(self, where: str, exc: Exception) -> None:
        with self._cond:
            self._counters["internal_errors"] += 1
        self._warn_once(f"internal-{where}", f"profiler internal error in {where}: {exc!r}")

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message)

    def _at_exit(self) -> None:
        # Skip if the last flush already timed out and nothing was delivered since: waiting again
        # would only double the host's exit delay for data that cannot be sent.
        try:
            if self._queue and not self._flush_timed_out:
                self.flush(self.flush_timeout_s)
        except Exception:
            pass
