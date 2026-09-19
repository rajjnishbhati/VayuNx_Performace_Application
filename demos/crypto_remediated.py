"""Crypto demo - REMEDIATED: the same signup/login flow with salted Argon2id password hashing.

Argon2id parameters (explicit, see README "Hashing algorithm choice"):
    time_cost=3, memory_cost=65536 KiB (64 MiB), parallelism=4, hash_len=32, salt=16 random bytes
= RFC 9106 section 4 "second recommended option" (also argon2-cffi's RFC_9106_LOW_MEMORY profile).

Span structure is IDENTICAL to crypto_baseline.py; only the hashing implementation and the
span attributes differ.

    python demos/crypto_remediated.py [--service-url http://127.0.0.1:8010] [--users 25] [--interval-ms 200]

Prints RUN_ID=<id> on success. All numbers come from actually running this script.
"""

from __future__ import annotations

import argparse
import hmac
import os
import unicodedata

from argon2.low_level import Type, hash_secret_raw

from vayunx_profiler_sdk import ProfilerClient

SERVICE_NAME = "crypto-demo"
LABEL = "argon2id-remediated"
TIME_COST, MEMORY_COST_KIB, PARALLELISM, HASH_LEN, SALT_LEN = 3, 65536, 4, 32, 16
ATTRS = {"algorithm": "Argon2id", "params": f"t={TIME_COST},m={MEMORY_COST_KIB}KiB,p={PARALLELISM}"}


def sanitize_input(password: str) -> bytes:
    return unicodedata.normalize("NFKC", password).strip().encode("utf-8")


def new_salt() -> bytes:
    return os.urandom(SALT_LEN)


def compute_digest(password_bytes: bytes, salt: bytes) -> bytes:
    return hash_secret_raw(password_bytes, salt, time_cost=TIME_COST, memory_cost=MEMORY_COST_KIB,
                           parallelism=PARALLELISM, hash_len=HASH_LEN, type=Type.ID)


def main(argv=None) -> str:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--service-url", default="http://127.0.0.1:8010")
    p.add_argument("--users", type=int, default=25)
    p.add_argument("--interval-ms", type=int, default=200)
    args = p.parse_args(argv)

    users = [(f"user{i:03d}", f"correct horse battery staple #{i}") for i in range(args.users)]
    user_db: dict[str, tuple[bytes, bytes]] = {}
    profiler = ProfilerClient(service_url=args.service_url, service_name=SERVICE_NAME)

    with profiler.run(label=LABEL, phase="remediated", metadata={"demo": "crypto_remediated.py", "users": args.users}) as run:
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
