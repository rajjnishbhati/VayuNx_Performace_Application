"""Built-in crypto presets (spec B). This is an allow-list: the Service may launch these and nothing else.

Each operation hashes a SYNTHETIC password - never real user data - with a fresh 16-byte salt where
the algorithm uses one (bcrypt generates its own 16-byte salt via gensalt()).
"""

from __future__ import annotations

import hashlib
import os
import ssl
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
from typing import Callable

SYNTHETIC_PASSWORD = b"vayunx-lab synthetic password 0001"  # 34 bytes: under bcrypt's 72-byte limit
SALT_LEN = 16
SCRYPT_MAXMEM = 256 * 1024 * 1024  # N=2^17, r=8 needs 128 MiB; hashlib refuses unless maxmem is above that


class PresetError(ValueError):
    pass


@dataclass(frozen=True)
class Preset:
    id: str
    label: str  # human variant name, e.g. "Argon2id m=64 MiB t=3 p=4"
    algorithm: str
    params: str  # canonical, e.g. "m=65536,t=3,p=4"
    family: str  # "fast-hash" | "password-hash"
    salted: bool
    library: str  # distribution name used for the version, or "hashlib"
    min_ops: int  # minimum operations per trial, for very slow variants
    builder: Callable[[], Callable[[], bytes]]

    def make_op(self) -> Callable[[], bytes]:
        return self.builder()

    def library_version(self) -> str:
        if self.library == "hashlib":
            return f"hashlib ({ssl.OPENSSL_VERSION})"
        try:
            return f"{self.library} {version(self.library)}"
        except PackageNotFoundError:
            return f"{self.library} (not installed)"

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "algorithm": self.algorithm, "params": self.params,
                "family": self.family, "salted": self.salted, "library": self.library_version()}


def _md5():
    return lambda: hashlib.md5(SYNTHETIC_PASSWORD).digest()


def _sha256():
    return lambda: hashlib.sha256(SYNTHETIC_PASSWORD).digest()


def _pbkdf2():
    return lambda: hashlib.pbkdf2_hmac("sha256", SYNTHETIC_PASSWORD, os.urandom(SALT_LEN), 600_000, dklen=32)


def _bcrypt(cost):
    def build():
        import bcrypt
        return lambda: bcrypt.hashpw(SYNTHETIC_PASSWORD, bcrypt.gensalt(rounds=cost))
    return build


def _scrypt():
    return lambda: hashlib.scrypt(SYNTHETIC_PASSWORD, salt=os.urandom(SALT_LEN), n=2 ** 17, r=8, p=1,
                                  maxmem=SCRYPT_MAXMEM, dklen=32)


def _argon2id(memory_kib, time_cost, parallelism):
    def build():
        from argon2.low_level import Type, hash_secret_raw
        return lambda: hash_secret_raw(SYNTHETIC_PASSWORD, os.urandom(SALT_LEN), time_cost=time_cost,
                                       memory_cost=memory_kib, parallelism=parallelism, hash_len=32, type=Type.ID)
    return build


_ALL = [
    Preset("md5", "MD5", "MD5", "", "fast-hash", False, "hashlib", 0, _md5),
    Preset("sha256", "SHA-256", "SHA-256", "", "fast-hash", False, "hashlib", 0, _sha256),
    Preset("pbkdf2-sha256-600k", "PBKDF2-HMAC-SHA256 600k", "PBKDF2-HMAC-SHA256", "iterations=600000",
           "password-hash", True, "hashlib", 10, _pbkdf2),
    Preset("bcrypt-10", "bcrypt cost 10", "bcrypt", "cost=10", "password-hash", True, "bcrypt", 10, _bcrypt(10)),
    Preset("bcrypt-12", "bcrypt cost 12", "bcrypt", "cost=12", "password-hash", True, "bcrypt", 10, _bcrypt(12)),
    Preset("scrypt-n17", "scrypt N=2^17 r=8 p=1", "scrypt", "N=131072,r=8,p=1", "password-hash", True, "hashlib", 10, _scrypt),
    Preset("argon2id-owasp", "Argon2id m=19 MiB t=2 p=1", "Argon2id", "m=19456,t=2,p=1", "password-hash", True,
           "argon2-cffi", 10, _argon2id(19456, 2, 1)),
    Preset("argon2id-rfc9106-low", "Argon2id m=64 MiB t=3 p=4", "Argon2id", "m=65536,t=3,p=4", "password-hash", True,
           "argon2-cffi", 10, _argon2id(65536, 3, 4)),
]
PRESETS: dict[str, Preset] = {p.id: p for p in _ALL}


def get_preset(preset_id: str) -> Preset:
    try:
        return PRESETS[preset_id]
    except (KeyError, TypeError):
        raise PresetError(f"unknown preset {preset_id!r}; built-in presets: {', '.join(PRESETS)}") from None
