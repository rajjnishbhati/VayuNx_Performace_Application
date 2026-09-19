"""VAYUNX Profiler SDK (Python) - thin wrapper over the Profiler Service's HTTP/JSON API.

    from vayunx_profiler_sdk import ProfilerClient

    profiler = ProfilerClient(service_url="http://localhost:8010", service_name="crypto-demo")
    with profiler.run(label="md5-baseline", phase="baseline") as run:
        profiler.start_sampling(interval_ms=500, metrics=["cpu_pct", "memory_mb"], category="cryptographic")
        with run.span("hash_password", category="cryptographic"):
            ...
        profiler.stop_sampling()

Spans are added manually around the code to measure; there is no automatic instrumentation.
"""

__version__ = "0.1.0"

from vayunx_profiler_sdk.client import ProfilerClient, ProfilerRun, SpanContext  # noqa: E402
from vayunx_profiler_sdk.sampling import SUPPORTED_METRICS  # noqa: E402
from vayunx_profiler_sdk.transport import ProfilerConnectionError, ProfilerHTTPError  # noqa: E402

__all__ = ["ProfilerClient", "ProfilerRun", "SpanContext", "SUPPORTED_METRICS", "ProfilerHTTPError", "ProfilerConnectionError"]
