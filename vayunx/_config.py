"""Settings from init() arguments, falling back to environment variables."""

from __future__ import annotations

import os
import sys
import uuid
from dataclasses import dataclass


def _num(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


@dataclass
class Config:
    endpoint: str
    service: str
    variant: str | None
    run_id: str
    phase: str | None
    slow_ms: float  # crypto calls at least this slow also become spans
    span_sample_every: int  # and every Nth call (0 = off)
    export_interval_s: float  # fast-path histogram shipping interval
    gauge_interval_s: float
    flush_timeout_s: float
    gauges: bool
    hooks: bool
    max_pending_exports: int = 60
    api_token: str | None = None  # VAYUNX_API_TOKEN: only when the Service requires sign-in

    def auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_token}"} if self.api_token else {}


def load(**overrides) -> Config:
    env = os.environ
    service = (overrides.get("service") or env.get("VAYUNX_SERVICE") or env.get("OTEL_SERVICE_NAME")
               or os.path.splitext(os.path.basename(sys.argv[0] or ""))[0] or "python-app")
    phase = overrides.get("phase") or env.get("VAYUNX_PHASE")
    cfg = Config(
        endpoint=(overrides.get("endpoint") or env.get("VAYUNX_ENDPOINT") or "http://127.0.0.1:8010").rstrip("/"),
        service=service,
        variant=overrides.get("variant") or env.get("VAYUNX_VARIANT") or None,
        run_id=overrides.get("run_id") or env.get("VAYUNX_RUN_ID") or uuid.uuid4().hex,
        phase=phase if phase in ("baseline", "remediated") else None,
        slow_ms=float(overrides["slow_ms"]) if overrides.get("slow_ms") is not None else _num("VAYUNX_SLOW_MS", 1.0),
        span_sample_every=int(overrides.get("span_sample_every") or _num("VAYUNX_SPAN_SAMPLE_EVERY", 0)),
        export_interval_s=float(overrides.get("export_interval_s") or _num("VAYUNX_EXPORT_INTERVAL_S", 5.0)),
        gauge_interval_s=float(overrides.get("gauge_interval_s") or _num("VAYUNX_GAUGE_INTERVAL_S", 1.0)),
        flush_timeout_s=float(overrides.get("flush_timeout_s") or _num("VAYUNX_FLUSH_TIMEOUT_S", 2.0)),
        gauges=overrides.get("gauges", env.get("VAYUNX_GAUGES", "1") != "0"),
        hooks=overrides.get("hooks", env.get("VAYUNX_HOOKS", "1") != "0"),
        api_token=overrides.get("api_token") or env.get("VAYUNX_API_TOKEN") or None,
    )
    return cfg


def disabled() -> bool:
    return os.environ.get("VAYUNX_DISABLE", "") in ("1", "true", "yes")
