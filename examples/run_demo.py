"""Runs an example app twice - VAYUNX_VARIANT=md5, then argon2id - under the profiler, drives login
traffic at each, and prints the link that opens the comparison under "My app".

    python examples/run_demo.py fastapi                      # service on http://127.0.0.1:8010
    python examples/run_demo.py next --seconds 30 --endpoint http://127.0.0.1:8011

Needs: the profiler service running; for `next`, `npm install && npm run build` in examples/next-login.
Each app process gets its own run id, so the two variants arrive as two runs of the same service.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import psutil

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import loadgen  # noqa: E402

APPS = {
    "fastapi": {"dir": HERE / "fastapi-login", "port": 8101, "service": "fastapi-login-example"},
    "next": {"dir": HERE / "next-login", "port": 8102, "service": "next-login-example"},
}


def command(app: str, port: int) -> list[str]:
    if app == "fastapi":  # launcher: hooks go in before the app imports anything
        return [sys.executable, "-m", "vayunx", "run", "--", sys.executable, "-m", "uvicorn", "app:app",
                "--host", "127.0.0.1", "--port", str(port), "--log-level", "warning"]
    # Next.js initialises the SDK itself from instrumentation.ts (register())
    return ["node", str(APPS["next"]["dir"] / "node_modules" / "next" / "dist" / "bin" / "next"), "start",
            "-p", str(port), "-H", "127.0.0.1"]


def wait_healthy(url: str, timeout: float = 60.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2) as r:
                return json.loads(r.read())
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"app did not become healthy at {url}")


def stop_tree(proc: subprocess.Popen) -> None:
    try:
        parent = psutil.Process(proc.pid)
        for p in parent.children(recursive=True) + [parent]:
            p.kill()
    except psutil.NoSuchProcess:
        pass
    proc.wait(timeout=10)


def run_variant(app: str, variant: str, endpoint: str, seconds: float, users: int, concurrency: int) -> tuple[str, dict]:
    cfg = APPS[app]
    run_id = uuid.uuid4().hex
    env = {**os.environ, "VAYUNX_ENDPOINT": endpoint, "VAYUNX_SERVICE": cfg["service"], "VAYUNX_VARIANT": variant,
           "VAYUNX_RUN_ID": run_id, "VAYUNX_EXPORT_INTERVAL_S": "1", "VAYUNX_EXPORT_INTERVAL_MS": "1000"}
    url = f"http://127.0.0.1:{cfg['port']}"
    proc = subprocess.Popen(command(app, cfg["port"]), cwd=cfg["dir"], env=env)
    try:
        health = wait_healthy(url)
        assert health["variant"] == variant, health
        client = loadgen.run(url, users=users, seconds=seconds, concurrency=concurrency)
        time.sleep(3.0)  # let the SDK's 1 s export cycle ship the tail before the process is stopped
    finally:
        stop_tree(proc)
    return run_id, client


def main() -> None:
    p = argparse.ArgumentParser(description="MD5 vs Argon2id in a real app, profiled")
    p.add_argument("app", choices=sorted(APPS))
    p.add_argument("--endpoint", default=os.environ.get("VAYUNX_ENDPOINT", "http://127.0.0.1:8010"))
    p.add_argument("--ui", default="http://127.0.0.1:3000")
    p.add_argument("--seconds", type=float, default=20.0)
    p.add_argument("--users", type=int, default=50)
    p.add_argument("--concurrency", type=int, default=4)
    a = p.parse_args()
    ids = []
    for variant in ("md5", "argon2id"):
        run_id, client = run_variant(a.app, variant, a.endpoint, a.seconds, a.users, a.concurrency)
        ids.append(run_id)
        print(f"{variant:9s} run {run_id}  client view: {json.dumps(client)}", flush=True)
    print(f"\ncompare in the UI:  {a.ui}/?runs={','.join(ids)}")
    print(f"compare via API:    {a.endpoint}/v2/compare?run_ids={','.join(ids)}")


if __name__ == "__main__":
    main()
