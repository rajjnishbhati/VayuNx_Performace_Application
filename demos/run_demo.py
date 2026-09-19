"""End-to-end MVP demo: start the Profiler Service if needed, run the three demo scripts,
print each comparison's generated summary and report URL.

    python demos/run_demo.py [--service-url http://127.0.0.1:8010] [--keep-service]

The crypto baseline and remediated scripts run in *separate* processes so each run's memory
samples start from a fresh interpreter.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url + "/healthz", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def run_script(name: str, *extra: str) -> str:
    cmd = [sys.executable, str(HERE / name), *extra]
    print(f"\n$ {' '.join(Path(c).name if i == 1 else c for i, c in enumerate(cmd))}", flush=True)
    proc = subprocess.run(cmd, capture_output=True, text=True, cwd=ROOT)
    print(proc.stdout.rstrip())
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"{name} failed with exit code {proc.returncode}")
    return proc.stdout


def show(url: str, title: str, baseline: str, remediated: str) -> None:
    q = urllib.parse.urlencode({"baseline_run_id": baseline, "remediated_run_id": remediated})
    with urllib.request.urlopen(f"{url}/v1/comparison?{q}", timeout=30) as r:
        payload = json.load(r)
    print(f"\n=== {title} ===\n{payload['summary']}")
    for w in payload["warnings"]:
        print(f"  ! {w}")
    print(f"  report: {url}/report?{q}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--service-url", default="http://127.0.0.1:8010")
    p.add_argument("--keep-service", action="store_true", help="leave a service started by this script running")
    p.add_argument("--users", default="25")
    p.add_argument("--interval-ms", default="200")
    args = p.parse_args(argv)
    url = args.service_url.rstrip("/")

    service = None
    if not healthy(url):
        print(f"Profiler Service not reachable at {url}; starting `python -m profiler_service` ...", flush=True)
        service = subprocess.Popen([sys.executable, "-m", "profiler_service"], cwd=ROOT)
        for _ in range(60):
            if healthy(url):
                break
            time.sleep(0.5)
        else:
            service.terminate()
            raise SystemExit("Profiler Service did not become healthy")
    try:
        common = ["--service-url", url, "--users", args.users, "--interval-ms", args.interval_ms]
        base = re.search(r"^RUN_ID=(\S+)", run_script("crypto_baseline.py", *common), re.M).group(1)
        rem = re.search(r"^RUN_ID=(\S+)", run_script("crypto_remediated.py", *common), re.M).group(1)
        out = run_script("general_file_read.py", "--service-url", url)
        g_base = re.search(r"^BASELINE_RUN_ID=(\S+)", out, re.M).group(1)
        g_rem = re.search(r"^REMEDIATED_RUN_ID=(\S+)", out, re.M).group(1)

        show(url, "cryptographic: md5-baseline vs argon2id-remediated", base, rem)
        show(url, "general: read-4kib-chunks vs read-1mib-chunks", g_base, g_rem)
        print(f"\nAll runs: {url}/")
        if service and args.keep_service:
            print(f"Service left running (pid {service.pid}). Stop it with Ctrl+C / kill.")
            service.wait()
    finally:
        if service and service.poll() is None and not args.keep_service:
            service.terminate()


if __name__ == "__main__":
    main()
