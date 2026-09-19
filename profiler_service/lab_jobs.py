"""Service-triggered Lab runs: one experiment at a time, on a background thread.

The Service can only launch built-in presets with validated parameters (ExperimentRunner validates
against vayunx_lab.presets); it never runs arbitrary code or commands.
"""

from __future__ import annotations

import sys
import threading

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from profiler_service.lab_store import ACTIVE, LabStore
from profiler_service.models import Experiment
from vayunx_lab.runner import ExperimentRunner


class Busy(RuntimeError):
    def __init__(self, experiment_id: str):
        super().__init__(f"experiment {experiment_id} is already running")
        self.experiment_id = experiment_id


class LabJobs:
    def __init__(self, Session: sessionmaker, python: str = sys.executable):
        self.Session = Session
        self.store = LabStore(Session)
        self.python = python
        self._lock = threading.Lock()
        self._current: tuple[str, ExperimentRunner, threading.Thread] | None = None

    def recover(self) -> int:
        """Mark experiments that a previous Service process left queued/running as failed."""
        with self.Session() as s:
            stale = s.scalars(select(Experiment).where(Experiment.status.in_(ACTIVE + ("cancelling",)))).all()
            ids = [e.experiment_id for e in stale]
        for exp_id in ids:
            self.store.set_status(exp_id, "failed", error="The service stopped while this experiment was running. "
                                                          "Start it again to get complete results.")
        return len(ids)

    def running(self) -> str | None:
        with self._lock:
            if self._current and self._current[2].is_alive():
                return self._current[0]
            return None

    def start(self, presets: list[str], *, trials: int, duration_s: float, concurrency: int,
              reference: str | None, warmup_s: float = 1.0, project_id: str = "default") -> str:
        with self._lock:
            if self._current and self._current[2].is_alive():
                raise Busy(self._current[0])
            runner = ExperimentRunner(self.store, presets, trials=trials, duration_s=duration_s, concurrency=concurrency,
                                      reference=reference, warmup_s=warmup_s, python=self.python,
                                      project_id=project_id)  # validates
            exp_id = runner.create()
            thread = threading.Thread(target=runner.run, args=(exp_id,), name=f"lab-{exp_id[:8]}", daemon=True)
            self._current = (exp_id, runner, thread)
            thread.start()
            return exp_id

    def cancel(self, exp_id: str) -> bool:
        with self._lock:
            if self._current and self._current[0] == exp_id and self._current[2].is_alive():
                self._current[1].cancel_event.set()
                return True
        return False

    def cancel_current(self) -> None:
        with self._lock:
            if self._current and self._current[2].is_alive():
                self._current[1].cancel_event.set()
