"""Configuration. Everything overridable by environment variable."""

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Swapping to Postgres later is a connection-string change:
#   VAYUNX_PROFILER_DB_URL=postgresql+psycopg://user:pass@host/db
DB_URL = os.environ.get("VAYUNX_PROFILER_DB_URL", f"sqlite:///{PROJECT_ROOT / 'profiler.db'}")
HOST = os.environ.get("VAYUNX_PROFILER_HOST", "127.0.0.1")
PORT = int(os.environ.get("VAYUNX_PROFILER_PORT", "8010"))

MAX_BATCH_ITEMS = 10_000  # per POST /v1/spans or /v1/samples request
MAX_ATTRIBUTES = 32

# Comparison / summary thresholds (documented in README).
MATERIAL_CHANGE_PCT = 5.0  # |% change| below this is described as "no material change"
MIN_SAMPLES_PER_METRIC = 3  # fewer samples than this -> sampling delta flagged as indicative only
DURATION_TOLERANCE_MS = 1.0  # allowed |(end - start) - duration_ms| before a consistency warning ...
DURATION_TOLERANCE_REL = 0.01  # ... or 1% of duration, whichever is larger
