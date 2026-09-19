"""Finding and describing the Node.js used by the Lab's Node runner (vayunx_lab/node/worker.mjs)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

NODE_DIR = Path(__file__).resolve().parent / "node"


def node_executable() -> str | None:
    """$VAYUNX_NODE, else `node` on PATH. None when Node.js is not installed."""
    explicit = os.environ.get("VAYUNX_NODE")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    return shutil.which("node")


def worker_path() -> Path:
    return NODE_DIR / "worker.mjs"


@lru_cache(maxsize=1)
def node_info() -> dict:
    """Version, OpenSSL and per-preset availability, as reported by the worker itself (`--list`)."""
    exe = node_executable()
    if exe is None:
        return {"available": False, "reason": "Node.js not found (install it, or set VAYUNX_NODE)", "presets": {}}
    try:
        out = subprocess.run([exe, str(worker_path()), "--list"], capture_output=True, text=True, timeout=60)
        info = json.loads(out.stdout.strip().splitlines()[-1])
        return {"available": True, "version": info["node"], "openssl": info["openssl"], "presets": info["presets"]}
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError) as exc:
        return {"available": False, "reason": f"the Node.js worker did not start ({type(exc).__name__})", "presets": {}}
