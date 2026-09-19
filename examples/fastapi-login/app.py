"""VAYUNX example: a FastAPI login service whose password hashing is chosen by VAYUNX_VARIANT.

    VAYUNX_VARIANT=md5       -> hashlib.md5            DEMO ONLY - never store passwords like this
    VAYUNX_VARIANT=argon2id  -> argon2-cffi, OWASP minimum parameters (m=19456 KiB, t=2, p=1)

Run it under the profiler without code changes:

    vayunx-run --service fastapi-login --variant md5 -- python -m uvicorn app:app --port 8101

The only profiler-specific lines are the two `scope(...)` blocks: they label the hashing done by
/register and /login, so the compare view matches that work across variants (and not other hashing).
Everything else - finding and timing the MD5 / Argon2id calls - is automatic.

Users live in memory; the load generator creates synthetic users and passwords.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import os
import threading

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

try:
    import vayunx
except ImportError:  # the app works without the profiler installed
    vayunx = None

VARIANT = os.environ.get("VAYUNX_VARIANT", "argon2id").strip().lower()

if VARIANT == "md5":
    def hash_password(password: str) -> str:
        return hashlib.md5(password.encode()).hexdigest()  # INSECURE: fast, unsalted - demo only

    def verify_password(stored: str, password: str) -> bool:
        return hmac.compare_digest(stored, hashlib.md5(password.encode()).hexdigest())

elif VARIANT == "argon2id":
    from argon2 import PasswordHasher
    from argon2.exceptions import VerificationError, VerifyMismatchError

    _ph = PasswordHasher(time_cost=2, memory_cost=19456, parallelism=1)  # OWASP Password Storage minimum

    def hash_password(password: str) -> str:
        return _ph.hash(password)

    def verify_password(stored: str, password: str) -> bool:
        try:
            return _ph.verify(stored, password)
        except (VerifyMismatchError, VerificationError):
            return False

else:
    raise SystemExit(f"VAYUNX_VARIANT must be 'md5' or 'argon2id', got {VARIANT!r}")


def scope(name: str):
    return vayunx.span(name) if vayunx is not None else contextlib.nullcontext()


class Credentials(BaseModel):
    username: str
    password: str


app = FastAPI(title=f"VAYUNX example login ({VARIANT})")
_users: dict[str, str] = {}
_lock = threading.Lock()


@app.get("/health")
def health() -> dict:
    return {"ok": True, "variant": VARIANT, "users": len(_users)}


# Plain `def` endpoints: FastAPI runs them in its threadpool, so a slow hash never blocks the event loop.
@app.post("/register", status_code=201)
def register(body: Credentials) -> dict:
    with scope("register"):
        stored = hash_password(body.password)
    with _lock:
        if body.username in _users:
            raise HTTPException(409, "user exists")
        _users[body.username] = stored
    return {"username": body.username}


@app.post("/login")
def login(body: Credentials) -> dict:
    stored = _users.get(body.username)
    if stored is None:
        raise HTTPException(401, "invalid credentials")
    with scope("login"):
        ok = verify_password(stored, body.password)
    if not ok:
        raise HTTPException(401, "invalid credentials")
    return {"ok": True}
