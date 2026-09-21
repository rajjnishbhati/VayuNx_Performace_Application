"""Built-in crypto presets (spec B). This is an allow-list: the Service may launch these and nothing else.

Each operation hashes a SYNTHETIC password - never real user data - with a fresh 16-byte salt where
the algorithm uses one (bcrypt generates its own 16-byte salt via gensalt()).
"""

from __future__ import annotations

import hashlib
import os
import ssl
from dataclasses import dataclass, replace
from importlib.metadata import PackageNotFoundError, version
from typing import Callable

SYNTHETIC_PASSWORD = b"vayunx-lab synthetic password 0001"  # 34 bytes: under bcrypt's 72-byte limit
SYNTHETIC_MESSAGE = b"vayunx-lab synthetic message 0001"  # what the signature presets sign; never real data
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
    family: str  # "fast-hash" | "password-hash" | "kem" | "signature"
    salted: bool
    library: str  # distribution name used for the version, or "hashlib"
    min_ops: int  # minimum operations per trial, for very slow variants
    builder: Callable[[], Callable[[], object]] | None
    runtime: str = "python"  # "python" (vayunx_lab/worker.py) or "node" (vayunx_lab/node/worker.mjs)
    operation: str = "hash"  # hash | keygen | encapsulate | decapsulate | sign | verify
    # What this operation puts on the wire, measured once outside the timing loop: (label, bytes).
    # Post-quantum migrations are usually a size problem as much as a speed one, so the size is a result.
    wire: Callable[[], tuple[str, int]] | None = None

    @property
    def base_id(self) -> str:
        """The built-in preset this variant runs ("md5" for "md5@node")."""
        return self.id.split("@", 1)[0]

    def make_op(self) -> Callable[[], object]:
        """The operation to time. Its return value is never used - it exists so the work cannot be skipped."""
        return self.builder()

    def wire_size(self) -> tuple[str, int] | None:
        """(what it is, how many bytes), from freshly generated material. None where nothing goes on the wire."""
        return self.wire() if self.wire else None

    def library_version(self) -> str:
        if self.runtime == "node":  # the worker reports the exact version with every result
            return "bcrypt (npm)" if self.library == "bcrypt" else "node:crypto"
        if self.library == "hashlib":
            return f"hashlib ({ssl.OPENSSL_VERSION})"
        try:
            return f"{self.library} {version(self.library)}"
        except PackageNotFoundError:
            return f"{self.library} (not installed)"

    def public(self) -> dict:
        return {"id": self.id, "label": self.label, "algorithm": self.algorithm, "params": self.params,
                "family": self.family, "salted": self.salted, "library": self.library_version(),
                "runtime": self.runtime, "operation": self.operation}


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


# --------------------------------------------------------------- post-quantum and the classical it replaces
#
# These measure ONE primitive each, with the key material built before the timing starts, because that is how
# a handshake spends it: a server generates a key once, then encapsulates or signs per connection. Comparing
# "ML-KEM-768 encapsulate" against "X25519 exchange" is the honest comparison; comparing a whole handshake
# against a password hash is not. Sizes matter as much as time here, so every preset reports what it puts on
# the wire (ML-DSA-44 signs in 2,420 bytes where ECDSA P-256 needs about 70).


def _mlkem768_keygen():
    from cryptography.hazmat.primitives.asymmetric import mlkem
    generate = mlkem.MLKEM768PrivateKey.generate
    return lambda: generate()


def _mlkem768_encapsulate():
    from cryptography.hazmat.primitives.asymmetric import mlkem
    public = mlkem.MLKEM768PrivateKey.generate().public_key()
    return lambda: public.encapsulate()


def _mlkem768_decapsulate():
    from cryptography.hazmat.primitives.asymmetric import mlkem
    private = mlkem.MLKEM768PrivateKey.generate()
    _, ciphertext = private.public_key().encapsulate()
    return lambda: private.decapsulate(ciphertext)


def _mlkem768_wire(what: str):
    def size():
        from cryptography.hazmat.primitives.asymmetric import mlkem
        private = mlkem.MLKEM768PrivateKey.generate()
        public = private.public_key()
        if what == "public key":
            return what, len(public.public_bytes_raw())
        _, ciphertext = public.encapsulate()
        return what, len(ciphertext)
    return size


def _mldsa(bits: str, verify: bool):
    def build():
        from cryptography.hazmat.primitives.asymmetric import mldsa
        private = (mldsa.MLDSA44PrivateKey if bits == "44" else mldsa.MLDSA65PrivateKey).generate()
        if not verify:
            return lambda: private.sign(SYNTHETIC_MESSAGE)
        public, signature = private.public_key(), private.sign(SYNTHETIC_MESSAGE)
        return lambda: public.verify(signature, SYNTHETIC_MESSAGE)
    return build


def _mldsa_wire(bits: str):
    def size():
        from cryptography.hazmat.primitives.asymmetric import mldsa
        private = (mldsa.MLDSA44PrivateKey if bits == "44" else mldsa.MLDSA65PrivateKey).generate()
        return "signature", len(private.sign(SYNTHETIC_MESSAGE))
    return size


def _x25519_keygen():
    from cryptography.hazmat.primitives.asymmetric import x25519
    generate = x25519.X25519PrivateKey.generate
    return lambda: generate()


def _x25519_exchange():
    from cryptography.hazmat.primitives.asymmetric import x25519
    private, peer = x25519.X25519PrivateKey.generate(), x25519.X25519PrivateKey.generate().public_key()
    return lambda: private.exchange(peer)


def _x25519_wire():
    from cryptography.hazmat.primitives.asymmetric import x25519
    return "public key", len(x25519.X25519PrivateKey.generate().public_key().public_bytes_raw())


def _ecdsa_p256(verify: bool):
    def build():
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        private, algorithm = ec.generate_private_key(ec.SECP256R1()), ec.ECDSA(hashes.SHA256())
        if not verify:
            return lambda: private.sign(SYNTHETIC_MESSAGE, algorithm)
        public, signature = private.public_key(), private.sign(SYNTHETIC_MESSAGE, algorithm)
        return lambda: public.verify(signature, SYNTHETIC_MESSAGE, algorithm)
    return build


def _ecdsa_p256_wire():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    private = ec.generate_private_key(ec.SECP256R1())
    # DER encoding, so 70-72 bytes depending on the r and s values; this is one real signature.
    return "signature (DER)", len(private.sign(SYNTHETIC_MESSAGE, ec.ECDSA(hashes.SHA256())))


def _rsa2048(verify: bool):
    def build():
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pad, digest = padding.PKCS1v15(), hashes.SHA256()  # what RS256 (JWT, TLS certificates) uses
        if not verify:
            return lambda: private.sign(SYNTHETIC_MESSAGE, pad, digest)
        public, signature = private.public_key(), private.sign(SYNTHETIC_MESSAGE, pad, digest)
        return lambda: public.verify(signature, SYNTHETIC_MESSAGE, pad, digest)
    return build


def _rsa2048_wire():
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return "signature", len(private.sign(SYNTHETIC_MESSAGE, padding.PKCS1v15(), hashes.SHA256()))


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

    # Key agreement: ML-KEM-768 (FIPS 203) against the X25519 it is paired with in TLS today.
    Preset("mlkem768-keygen", "ML-KEM-768 keygen", "ML-KEM-768", "level=3", "kem", False, "cryptography", 0,
           _mlkem768_keygen, operation="keygen", wire=_mlkem768_wire("public key")),
    Preset("mlkem768-encap", "ML-KEM-768 encapsulate", "ML-KEM-768", "level=3", "kem", False, "cryptography", 0,
           _mlkem768_encapsulate, operation="encapsulate", wire=_mlkem768_wire("ciphertext")),
    Preset("mlkem768-decap", "ML-KEM-768 decapsulate", "ML-KEM-768", "level=3", "kem", False, "cryptography", 0,
           _mlkem768_decapsulate, operation="decapsulate", wire=_mlkem768_wire("ciphertext")),
    Preset("x25519-keygen", "X25519 keygen", "X25519", "", "kem", False, "cryptography", 0,
           _x25519_keygen, operation="keygen", wire=_x25519_wire),
    Preset("x25519-exchange", "X25519 exchange", "X25519", "", "kem", False, "cryptography", 0,
           _x25519_exchange, operation="encapsulate", wire=_x25519_wire),

    # Signatures: ML-DSA (FIPS 204) against the ECDSA and RSA that sign certificates and tokens today.
    Preset("mldsa44-sign", "ML-DSA-44 sign", "ML-DSA-44", "level=2", "signature", False, "cryptography", 0,
           _mldsa("44", False), operation="sign", wire=_mldsa_wire("44")),
    Preset("mldsa44-verify", "ML-DSA-44 verify", "ML-DSA-44", "level=2", "signature", False, "cryptography", 0,
           _mldsa("44", True), operation="verify", wire=_mldsa_wire("44")),
    Preset("mldsa65-sign", "ML-DSA-65 sign", "ML-DSA-65", "level=3", "signature", False, "cryptography", 0,
           _mldsa("65", False), operation="sign", wire=_mldsa_wire("65")),
    Preset("mldsa65-verify", "ML-DSA-65 verify", "ML-DSA-65", "level=3", "signature", False, "cryptography", 0,
           _mldsa("65", True), operation="verify", wire=_mldsa_wire("65")),
    Preset("ecdsa-p256-sign", "ECDSA P-256 sign", "ECDSA P-256", "hash=SHA-256", "signature", False, "cryptography", 0,
           _ecdsa_p256(False), operation="sign", wire=_ecdsa_p256_wire),
    Preset("ecdsa-p256-verify", "ECDSA P-256 verify", "ECDSA P-256", "hash=SHA-256", "signature", False, "cryptography", 0,
           _ecdsa_p256(True), operation="verify", wire=_ecdsa_p256_wire),
    Preset("rsa2048-sign", "RSA-2048 sign", "RSA-2048 PKCS#1 v1.5", "hash=SHA-256", "signature", False, "cryptography", 0,
           _rsa2048(False), operation="sign", wire=_rsa2048_wire),
    Preset("rsa2048-verify", "RSA-2048 verify", "RSA-2048 PKCS#1 v1.5", "hash=SHA-256", "signature", False, "cryptography", 0,
           _rsa2048(True), operation="verify", wire=_rsa2048_wire),
]
PRESETS: dict[str, Preset] = {p.id: p for p in _ALL}


RUNTIMES = ("python", "node")
RUNTIME_LABELS = {"python": "Python", "node": "Node.js"}


def split_variant(variant_id: str) -> tuple[str, str]:
    """"md5" -> ("md5", "python"); "md5@node" -> ("md5", "node"). Raises PresetError otherwise."""
    if not isinstance(variant_id, str):
        raise PresetError(f"unknown preset {variant_id!r}; built-in presets: {', '.join(PRESETS)}")
    base, sep, runtime = variant_id.partition("@")
    if not sep:
        return base, "python"
    if runtime not in RUNTIMES or runtime == "python":
        raise PresetError(f"unknown runtime in {variant_id!r}; use <preset> for Python or <preset>@node for Node.js")
    return base, runtime


def get_preset(preset_id: str) -> Preset:
    """A built-in preset, or its Node.js twin ("<preset>@node"): same algorithm and parameters, other runtime."""
    base_id, runtime = split_variant(preset_id)
    try:
        base = PRESETS[base_id]
    except KeyError:
        raise PresetError(f"unknown preset {preset_id!r}; built-in presets: {', '.join(PRESETS)} "
                          f"(add @node to run one in Node.js)") from None
    if runtime == "python":
        return base
    return replace(base, id=preset_id, label=f"{base.label} · {RUNTIME_LABELS[runtime]}", runtime=runtime, builder=None)
