"""VAYUNX Profiler SDK (Python) - thin wrapper over the Profiler Service's HTTP/JSON API.

    from vayunx_profiler_sdk import ProfilerClient

    profiler = ProfilerClient(service_url="http://localhost:8010", service_name="crypto-demo")
    with profiler.run(label="md5-baseline", phase="baseline") as run:
        profiler.start_sampling(interval_ms=500, metrics=["cpu_pct", "memory_mb"], category="cryptographic")
        with run.span("hash_password", category="cryptographic"):   # full span (a few us each)
            ...
        md5 = run.op("md5_hash", attributes={"crypto.algorithm": "MD5"})  # fast path: histogram, ~1 us
        with md5:
            ...
        profiler.stop_sampling()

Fail-safe: profiler errors never reach the application; see client.py and README "SDK safety".
Spans are added manually around the code to measure; there is no automatic instrumentation.
"""

__version__ = "0.2.0"

from vayunx_profiler_sdk.client import ProfilerClient, ProfilerRun, SpanContext  # noqa: E402
from vayunx_profiler_sdk.ops import Op  # noqa: E402
from vayunx_profiler_sdk.sampling import SUPPORTED_METRICS  # noqa: E402
from vayunx_profiler_sdk.transport import ProfilerConnectionError, ProfilerHTTPError  # noqa: E402

__all__ = ["ProfilerClient", "ProfilerRun", "SpanContext", "Op", "SUPPORTED_METRICS",
           "ProfilerHTTPError", "ProfilerConnectionError"]
