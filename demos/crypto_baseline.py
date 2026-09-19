"""Crypto demo - BASELINE: signup/login flow with unsalted, single-pass MD5 password hashing.

*** DELIBERATELY INSECURE. MD5 is broken for password storage. Demo fixture only - never copy. ***

The span structure is IDENTICAL to crypto_remediated.py, so the Profiler Service can match spans
by name + position in the tree. The hashing algorithm is recorded as a span *attribute*
({"algorithm": "MD5"}), not in the span name - otherwise the key span would appear as
"present in one run only" instead of producing a delta.

    python demos/crypto_baseline.py [--service-url http://127.0.0.1:8010] [--users 25] [--interval-ms 200]

Prints RUN_ID=<id> on success. All numbers come from actually running this script.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import unicodedata

from vayunx_profiler_sdk import ProfilerClient

SERVICE_NAME = "crypto-demo"
LABEL = "md5-baseline"
ATTRS = {"algorithm": "MD5", "params": "unsalted single pass"}


def sanitize_input(password: str) -> bytes:
    return unicodedata.normalize("NFKC", password).strip().encode("utf-8")


def new_salt() -> bytes:
    return b""  # baseline anti-pattern: no salt


def compute_digest(password_bytes: bytes, salt: bytes) -> bytes:
    # Unsalted single-pass MD5; `salt` is deliberately unused (baseline anti-pattern).
    return hashlib.md5(password_bytes).digest()


def main(argv=None) -> str:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--service-url", default="http://127.0.0.1:8010")
    p.add_argument("--users", type=int, default=25)
    p.add_argument("--interval-ms", type=int, default=200)
    args = p.parse_args(argv)

    users = [(f"user{i:03d}", f"correct horse battery staple #{i}") for i in range(args.users)]
    user_db: dict[str, tuple[bytes, bytes]] = {}
    profiler = ProfilerClient(service_url=args.service_url, service_name=SERVICE_NAME)

    with profiler.run(label=LABEL, phase="baseline", metadata={"demo": "crypto_baseline.py", "users": args.users}) as run:
        profiler.start_sampling(interval_ms=args.interval_ms, metrics=["cpu_pct", "memory_mb"], category="cryptographic")

        for username, password in users:
            with run.span("signup", category="general"):
                with run.span("hash_password", category="cryptographic", attributes=ATTRS):
                    with run.span("sanitize_input", category="cryptographic"):
                        pw = sanitize_input(password)
                    with run.span("compute_digest", category="cryptographic", attributes=ATTRS):
                        salt = new_salt()
                        digest = compute_digest(pw, salt)
                with run.span("store_user", category="general"):
                    user_db[username] = (salt, digest)

        for username, password in users:
            with run.span("login", category="general"):
                with run.span("load_user", category="general"):
                    salt, stored = user_db[username]
                with run.span("hash_password", category="cryptographic", attributes=ATTRS):
                    with run.span("sanitize_input", category="cryptographic"):
                        pw = sanitize_input(password)
                    with run.span("compute_digest", category="cryptographic", attributes=ATTRS):
                        digest = compute_digest(pw, salt)
                with run.span("check_credentials", category="cryptographic"):
                    ok = hmac.compare_digest(digest, stored)
                if not ok:
                    raise SystemExit(f"login failed for {username}")

        profiler.stop_sampling()

    # delivery is asynchronous; run exit flushes (bounded), so these are the service-accepted counts
    print(f"spans_sent={run.spans_sent} samples_sent={run.samples_sent} sdk_stats={profiler.stats()}")
    print(f"RUN_ID={run.run_id}")
    return run.run_id


if __name__ == "__main__":
    main()
