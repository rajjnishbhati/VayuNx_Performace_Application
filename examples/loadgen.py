"""Load generator for the example login apps (stdlib only).

    python examples/loadgen.py --url http://127.0.0.1:8101 --users 50 --seconds 20 --concurrency 4

Registers `--users` synthetic users, then logs in random users from `--concurrency` threads for
`--seconds`, and prints what the client measured (requests, rate, latency percentiles, errors).
These are client-side numbers for the whole HTTP request; the profiler reports the hashing itself.
"""

from __future__ import annotations

import argparse
import http.client
import json
import random
import threading
import time
from urllib.parse import urlparse


def _conn(url: str) -> http.client.HTTPConnection:
    u = urlparse(url)
    return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=60)


def _post(conn: http.client.HTTPConnection, path: str, body: dict) -> int:
    conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    r = conn.getresponse()
    r.read()
    return r.status


def run(url: str, users: int = 50, seconds: float = 20.0, concurrency: int = 4, seed: int = 1) -> dict:
    rnd = random.Random(seed)
    creds = [(f"user{i}", f"pw-{i}-{rnd.randrange(10**9)}") for i in range(users)]
    conn = _conn(url)
    reg_errors = sum(_post(conn, "/register", {"username": u, "password": p}) not in (201, 409) for u, p in creds)
    conn.close()

    latencies: list[float] = []
    errors = [0]
    lock = threading.Lock()
    deadline = time.monotonic() + seconds

    def worker(k: int) -> None:
        r = random.Random(seed * 1000 + k)
        c = _conn(url)
        mine: list[float] = []
        bad = 0
        while time.monotonic() < deadline:
            u, p = r.choice(creds)
            t0 = time.perf_counter()
            try:
                status = _post(c, "/login", {"username": u, "password": p})
            except (OSError, http.client.HTTPException):
                c.close()
                c = _conn(url)
                status = 0
            mine.append(time.perf_counter() - t0)
            bad += status != 200
        c.close()
        with lock:
            latencies.extend(mine)
            errors[0] += bad

    threads = [threading.Thread(target=worker, args=(k,)) for k in range(concurrency)]
    t0 = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - t0
    lat = sorted(latencies)
    pct = (lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))] * 1000, 2)) if lat else (lambda q: None)
    return {"logins": len(lat), "seconds": round(elapsed, 1), "logins_per_s": round(len(lat) / elapsed, 1),
            "p50_ms": pct(0.50), "p95_ms": pct(0.95), "p99_ms": pct(0.99), "errors": errors[0],
            "register_errors": reg_errors, "concurrency": concurrency}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url", required=True)
    p.add_argument("--users", type=int, default=50)
    p.add_argument("--seconds", type=float, default=20.0)
    p.add_argument("--concurrency", type=int, default=4)
    a = p.parse_args()
    print(json.dumps(run(a.url, a.users, a.seconds, a.concurrency), indent=2))


if __name__ == "__main__":
    main()
