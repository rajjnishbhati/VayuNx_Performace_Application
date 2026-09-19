"""VAYUNX Crypto Profiler - Python SDK on OpenTelemetry (spec F).

No code changes needed: run your app with the launcher and every supported crypto call is measured.

    vayunx-run python app.py                      # or: python -m vayunx run -- python app.py
    VAYUNX_VARIANT=argon2id vayunx-run python app.py

Or initialise explicitly (as early as possible, before crypto libraries are imported elsewhere):

    import vayunx
    vayunx.init(service="my-app", variant="md5")   # endpoint defaults to http://127.0.0.1:8010

    @vayunx.measure("login")
    def login(...): ...

    with vayunx.span("checkout"):
        ...

Safety contract (spec A): the profiler never raises into your code, never masks your exceptions, never
does network I/O on your threads, bounds its memory, and never records passwords, keys, salts, hashes or
tokens - only sizes, parameters and timings.
"""

__version__ = "0.3.0"

from vayunx._core import init, measure, shutdown, span, status  # noqa: E402

__all__ = ["init", "shutdown", "span", "measure", "status", "__version__"]
