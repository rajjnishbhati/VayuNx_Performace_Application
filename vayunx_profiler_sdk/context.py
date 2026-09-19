"""The innermost open span for the current thread / asyncio task: (run_id, span_id)."""

from contextvars import ContextVar

current_span: ContextVar[tuple[str, str] | None] = ContextVar("vayunx_profiler_current_span", default=None)
