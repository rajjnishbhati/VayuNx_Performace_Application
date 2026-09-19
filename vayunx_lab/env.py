"""Environment fingerprint: every Lab number is labelled with the machine and runtime that produced it."""

from __future__ import annotations

import hashlib
import json
import platform
import ssl
import subprocess
import sys
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version

import psutil


def _cpu_model() -> str:
    try:
        if sys.platform == "win32":
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
                return winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5)
            if out.stdout.strip():
                return out.stdout.strip()
        with open("/proc/cpuinfo", encoding="utf-8") as f:
            for line in f:
                if line.lower().startswith("model name"):
                    return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return platform.processor() or "unknown"


def _v(dist: str) -> str | None:
    try:
        return version(dist)
    except PackageNotFoundError:
        return None


@lru_cache(maxsize=1)
def fingerprint() -> dict:
    env = {
        "cpu_model": _cpu_model(),
        "cpu_count_physical": psutil.cpu_count(logical=False),
        "cpu_count_logical": psutil.cpu_count(logical=True),
        "ram_total_bytes": psutil.virtual_memory().total,
        "os": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        "openssl": ssl.OPENSSL_VERSION,
        "libraries": {d: _v(d) for d in ("argon2-cffi", "bcrypt", "psutil")},
        "runtime": f"python {platform.python_version()}",
    }
    env["id"] = hashlib.sha256(json.dumps(env, sort_keys=True).encode()).hexdigest()[:16]
    return env
