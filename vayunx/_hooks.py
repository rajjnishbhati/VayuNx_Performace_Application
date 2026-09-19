"""Automatic crypto hooks: which functions are wrapped, and how each call is described.

A describer sees the call's arguments and returns (algorithm, params, input_bytes). It may read sizes
and public cost parameters (e.g. "m=19456,t=2,p=1" from a PHC string's parameter field, or the bcrypt
cost digits) and nothing else - no password, key, salt, hash or token value ever leaves this module.

Libraries not yet imported are hooked when they are imported (post-import hook on sys.meta_path).
Nested hooked calls (argon2 inside passlib, SHA-256 inside HMAC) are recorded once, as the outer call.
References bound before hooks were installed (`from hashlib import md5` above `vayunx.init()`) are not
seen - use the launcher (`vayunx-run`) so hooks go in before the app imports anything.
"""

from __future__ import annotations

import base64
import functools
import importlib.util
import json
import sys
import threading
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter_ns
from types import FunctionType
from typing import Callable

from vayunx._cpuclock import thread_cpu_ns
from vayunx._ship import QUEUE_MAX

# ----------------------------------------------------------------------------- describers


def _len(value) -> int | None:
    if isinstance(value, str):
        return len(value.encode("utf-8", "surrogatepass"))
    try:
        return memoryview(value).nbytes
    except TypeError:
        return None


def _arg(args, kwargs, index: int, name: str, default=None):
    if len(args) > index:
        return args[index]
    return kwargs.get(name, default)


_HASH_NAMES = {"md5": "MD5", "sha1": "SHA1", "sha224": "SHA224", "sha256": "SHA256", "sha384": "SHA384",
               "sha512": "SHA512", "sha512_224": "SHA512/224", "sha512_256": "SHA512/256", "sha3_224": "SHA3-224",
               "sha3_256": "SHA3-256", "sha3_384": "SHA3-384", "sha3_512": "SHA3-512", "shake_128": "SHAKE128",
               "shake_256": "SHAKE256", "blake2b": "BLAKE2b", "blake2s": "BLAKE2s", "sm3": "SM3", "md5-sha1": "MD5-SHA1"}


_hash_name_cache: dict = {}


def hash_name(value) -> str:
    """'sha256' / hashlib.sha256 / _hashlib.openssl_sha256 / a module with digest_size -> 'SHA256'."""
    try:
        return _hash_name_cache[value]
    except (KeyError, TypeError):
        pass
    name = _hash_name(value)
    try:
        if len(_hash_name_cache) < 256:
            _hash_name_cache[value] = name
    except TypeError:
        pass
    return name


def _hash_name(value) -> str:
    if isinstance(value, str):
        name = value
    else:
        name = getattr(value, "__name__", None) or getattr(value, "name", None) or type(value).__name__
        name = str(name).rsplit(".", 1)[-1]
        if name.startswith("openssl_"):
            name = name[len("openssl_"):]
    key = name.lower().replace("-", "_")
    return _HASH_NAMES.get(key, name.upper())


def _constructor(algorithm: str):
    def describe(args, kwargs):
        data = _arg(args, kwargs, 0, "data") if args or "data" in kwargs else _arg(args, kwargs, 0, "string")
        return algorithm, None, (_len(data) if data is not None else 0)
    return describe


def _hashlib_new(args, kwargs):
    data = _arg(args, kwargs, 1, "data")
    if data is None:
        data = kwargs.get("string")
    return hash_name(_arg(args, kwargs, 0, "name", "?")), None, (_len(data) if data is not None else 0)


def _hashlib_new_key(args, kwargs):
    return hash_name(args[0] if args else kwargs.get("name", "?")), None


def _pbkdf2(args, kwargs):
    alg = hash_name(_arg(args, kwargs, 0, "hash_name", "?"))
    return f"PBKDF2-HMAC-{alg}", f"i={int(_arg(args, kwargs, 3, 'iterations', 0))}", _len(_arg(args, kwargs, 1, "password"))


def _hashlib_scrypt(args, kwargs):
    return "scrypt", f"n={kwargs.get('n')},r={kwargs.get('r')},p={kwargs.get('p')}", _len(_arg(args, kwargs, 0, "password"))


def _hmac_new(args, kwargs):
    msg = _arg(args, kwargs, 1, "msg")
    return f"HMAC-{hash_name(_arg(args, kwargs, 2, 'digestmod', '?'))}", None, (_len(msg) if msg is not None else 0)


_hmac_names: dict = {}


def _hmac_alg(digestmod) -> str:
    try:
        return _hmac_names[digestmod]
    except (KeyError, TypeError):
        name = f"HMAC-{hash_name(digestmod)}"
        try:
            if len(_hmac_names) < 256:
                _hmac_names[digestmod] = name
        except TypeError:
            pass
        return name


def _hmac_new_key(args, kwargs):
    return _hmac_alg(args[2] if len(args) > 2 else kwargs.get("digestmod", "?")), None


def _hmac_digest_key(args, kwargs):
    return _hmac_alg(args[2] if len(args) > 2 else kwargs.get("digest", "?")), None


def _hmac_digest(args, kwargs):
    return f"HMAC-{hash_name(_arg(args, kwargs, 2, 'digest', '?'))}", None, _len(_arg(args, kwargs, 1, "msg"))


def _bcrypt_cost(setting) -> str | None:
    """The two cost digits of a bcrypt salt/hash ('$2b$12$...') - public, not secret."""
    try:
        s = setting.decode("ascii", "replace") if isinstance(setting, (bytes, bytearray)) else str(setting)
        if len(s) >= 7 and s[0] == "$" and s[3] == "$" and s[4:6].isdigit() and s[6] == "$":
            return f"cost={int(s[4:6])}"
    except Exception:
        pass
    return None


def _bcrypt_hashpw(args, kwargs):
    return "bcrypt", _bcrypt_cost(_arg(args, kwargs, 1, "salt")), _len(_arg(args, kwargs, 0, "password"))


def _bcrypt_checkpw(args, kwargs):
    return "bcrypt", _bcrypt_cost(_arg(args, kwargs, 1, "hashed_password")), _len(_arg(args, kwargs, 0, "password"))


def _bcrypt_kdf(args, kwargs):
    return "bcrypt-pbkdf", f"rounds={_arg(args, kwargs, 3, 'rounds')}", _len(_arg(args, kwargs, 0, "password"))


_ARGON2_TYPES = {"argon2id": "Argon2id", "argon2i": "Argon2i", "argon2d": "Argon2d"}


def _phc(encoded) -> tuple[str | None, str | None]:
    """Algorithm and cost parameters from a PHC string ('$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>').
    Only the 1st and parameter fields are read; salt and hash fields are ignored."""
    try:
        s = encoded.decode("ascii", "replace") if isinstance(encoded, (bytes, bytearray)) else str(encoded)
        parts = s.split("$")
        if len(parts) < 4 or parts[0] != "":
            return None, None
        alg = _ARGON2_TYPES.get(parts[1], parts[1][:24])
        params = next((p for p in parts[2:4] if p.startswith("m=")), None)
        return alg, (params[:64] if params else None)
    except Exception:
        return None, None


def _argon2_type(value) -> str:
    name = getattr(value, "name", str(value)).upper()
    return {"ID": "Argon2id", "I": "Argon2i", "D": "Argon2d"}.get(name, "Argon2")


def _ph_hash(args, kwargs):
    ph = args[0]
    params = f"m={ph.memory_cost},t={ph.time_cost},p={ph.parallelism}"
    return _argon2_type(ph.type), params, _len(_arg(args, kwargs, 1, "password"))


def _ph_verify(args, kwargs):
    alg, params = _phc(_arg(args, kwargs, 1, "hash"))
    return alg or "Argon2", params, _len(_arg(args, kwargs, 2, "password"))


def _argon2_low(args, kwargs):
    m, t, p = kwargs.get("memory_cost"), kwargs.get("time_cost"), kwargs.get("parallelism")
    return _argon2_type(kwargs.get("type", "ID")), f"m={m},t={t},p={p}", _len(_arg(args, kwargs, 0, "secret"))


def _argon2_low_verify(args, kwargs):
    alg, params = _phc(_arg(args, kwargs, 0, "hash"))
    return alg or _argon2_type(_arg(args, kwargs, 2, "type", "ID")), params, _len(_arg(args, kwargs, 1, "secret"))


def _passlib_hash(args, kwargs):
    ctx = args[0]
    scheme = kwargs.get("scheme") or (args[2] if len(args) > 2 else None)
    try:
        scheme = scheme or ctx.default_scheme()
    except Exception:
        scheme = scheme or "passlib"
    return f"passlib:{scheme}", None, _len(_arg(args, kwargs, 1, "secret"))


def _passlib_verify(args, kwargs):
    ctx, hashed = args[0], _arg(args, kwargs, 2, "hash")
    try:
        scheme = ctx.identify(hashed) or "unknown"
    except Exception:
        scheme = "unknown"
    alg, params = _phc(hashed)
    return f"passlib:{scheme}", params or _bcrypt_cost(hashed), _len(_arg(args, kwargs, 1, "secret"))


def _nacl_str(algorithm: str):
    def describe(args, kwargs):
        ops, mem = _arg(args, kwargs, 1, "opslimit"), _arg(args, kwargs, 2, "memlimit")
        params = f"ops={ops},mem={mem}" if ops is not None else None
        return algorithm, params, _len(_arg(args, kwargs, 0, "password"))
    return describe


def _nacl_verify(args, kwargs):
    stored = _arg(args, kwargs, 0, "password_hash")
    alg, params = _phc(stored)
    if alg is None and isinstance(stored, (bytes, bytearray)) and stored[:3] == b"$7$":
        alg = "scrypt"
    return alg or "libsodium-pwhash", params, _len(_arg(args, kwargs, 1, "password"))


def _ed25519_sign(args, kwargs):
    return "Ed25519", None, _len(_arg(args, kwargs, 1, "message"))


def _ed25519_verify(args, kwargs):
    return "Ed25519", None, _len(_arg(args, kwargs, 1, "smessage"))


def _jwt_encode(args, kwargs):
    return str(_arg(args, kwargs, 2, "algorithm", "HS256") or "none")[:16], None, None


def _jwt_decode(args, kwargs):
    token = _arg(args, kwargs, 0, "jwt")
    alg = "JWT"
    try:  # only the header ('alg' field) is read, never the claims or signature
        header = (token.decode() if isinstance(token, bytes) else str(token)).split(".", 1)[0]
        alg = str(json.loads(base64.urlsafe_b64decode(header + "=" * (-len(header) % 4))).get("alg", "JWT"))[:16]
    except Exception:
        pass
    return alg, None, _len(token)


def _fernet(args, kwargs):
    return "Fernet", None, _len(_arg(args, kwargs, 1, "data"))


def _crypto_pbkdf2(args, kwargs):
    # cryptography >= 42 builds PBKDF2HMAC/Scrypt in Rust and exposes no parameters after construction
    # (only derive/derive_into/verify), so the hash and iteration count are reported only when readable.
    kdf = args[0]
    alg = getattr(getattr(kdf, "_algorithm", None), "name", None)
    iters = getattr(kdf, "_iterations", None)
    return (f"PBKDF2-HMAC-{hash_name(alg)}" if alg else "PBKDF2-HMAC"), (f"i={iters}" if iters else None),         _len(_arg(args, kwargs, 1, "key_material"))


def _crypto_scrypt(args, kwargs):
    kdf = args[0]
    n, r, p = (getattr(kdf, a, None) for a in ("_n", "_r", "_p"))
    return "scrypt", (f"n={n},r={r},p={p}" if n else None), _len(_arg(args, kwargs, 1, "key_material"))


def _aead(algorithm: str):
    def describe(args, kwargs):
        return algorithm, None, _len(_arg(args, kwargs, 2, "data"))
    return describe


# ----------------------------------------------------------------------------- registry


@dataclass(eq=False)
class Spec:
    name: str  # public hook name, e.g. "hashlib.md5"
    module: str  # module that owns the target
    path: str  # attribute path inside the module ("md5", "PasswordHasher.hash")
    operation: str  # hash / verify / kdf / mac / sign / encrypt / decrypt
    dist: str | None  # distribution name for the version label ("argon2-cffi"); None = stdlib
    describe: Callable
    slow: bool = False  # password hashing / KDFs: read CPU time on every call
    fixed: tuple | None = None  # (algorithm, params) when the call's arguments cannot change them
    keyfn: Callable | None = None  # cheap (algorithm, params) for the histogram key; default describe()[:2]
    library: str = ""  # filled at install: "argon2-cffi 25.1.0"
    keys: dict = field(default_factory=dict)  # (algorithm, params) -> histogram key cache


def _specs() -> list[Spec]:
    s: list[Spec] = []
    for ctor in ("md5", "sha1", "sha224", "sha256", "sha384", "sha512", "sha3_256", "sha3_512", "blake2b", "blake2s"):
        s.append(Spec(f"hashlib.{ctor}", "hashlib", ctor, "hash", None, _constructor(_HASH_NAMES[ctor]),
                      fixed=(_HASH_NAMES[ctor], None)))
    s += [
        Spec("hashlib.new", "hashlib", "new", "hash", None, _hashlib_new, keyfn=_hashlib_new_key),
        Spec("hashlib.pbkdf2_hmac", "hashlib", "pbkdf2_hmac", "kdf", None, _pbkdf2, slow=True),
        Spec("hashlib.scrypt", "hashlib", "scrypt", "kdf", None, _hashlib_scrypt, slow=True),
        Spec("hmac.new", "hmac", "new", "mac", None, _hmac_new, keyfn=_hmac_new_key),
        Spec("hmac.digest", "hmac", "digest", "mac", None, _hmac_digest, keyfn=_hmac_digest_key),
        Spec("bcrypt.hashpw", "bcrypt", "hashpw", "hash", "bcrypt", _bcrypt_hashpw, slow=True),
        Spec("bcrypt.checkpw", "bcrypt", "checkpw", "verify", "bcrypt", _bcrypt_checkpw, slow=True),
        Spec("bcrypt.kdf", "bcrypt", "kdf", "kdf", "bcrypt", _bcrypt_kdf, slow=True),
        Spec("argon2.PasswordHasher.hash", "argon2", "PasswordHasher.hash", "hash", "argon2-cffi", _ph_hash, slow=True),
        Spec("argon2.PasswordHasher.verify", "argon2", "PasswordHasher.verify", "verify", "argon2-cffi", _ph_verify, slow=True),
        Spec("argon2.low_level.hash_secret", "argon2.low_level", "hash_secret", "hash", "argon2-cffi", _argon2_low, slow=True),
        Spec("argon2.low_level.hash_secret_raw", "argon2.low_level", "hash_secret_raw", "kdf", "argon2-cffi", _argon2_low, slow=True),
        Spec("argon2.low_level.verify_secret", "argon2.low_level", "verify_secret", "verify", "argon2-cffi", _argon2_low_verify, slow=True),
        Spec("passlib.CryptContext.hash", "passlib.context", "CryptContext.hash", "hash", "passlib", _passlib_hash, slow=True),
        Spec("passlib.CryptContext.verify", "passlib.context", "CryptContext.verify", "verify", "passlib", _passlib_verify, slow=True),
        Spec("nacl.pwhash.str", "nacl.pwhash", "str", "hash", "PyNaCl", _nacl_str("Argon2id"), slow=True),
        Spec("nacl.pwhash.verify", "nacl.pwhash", "verify", "verify", "PyNaCl", _nacl_verify, slow=True),
        Spec("nacl.pwhash.argon2id.str", "nacl.pwhash.argon2id", "str", "hash", "PyNaCl", _nacl_str("Argon2id"), slow=True),
        Spec("nacl.pwhash.argon2id.verify", "nacl.pwhash.argon2id", "verify", "verify", "PyNaCl", _nacl_verify, slow=True),
        Spec("nacl.pwhash.scrypt.str", "nacl.pwhash.scrypt", "str", "hash", "PyNaCl", _nacl_str("scrypt"), slow=True),
        Spec("nacl.signing.SigningKey.sign", "nacl.signing", "SigningKey.sign", "sign", "PyNaCl", _ed25519_sign),
        Spec("nacl.signing.VerifyKey.verify", "nacl.signing", "VerifyKey.verify", "verify", "PyNaCl", _ed25519_verify),
        Spec("jwt.encode", "jwt", "encode", "sign", "PyJWT", _jwt_encode),
        Spec("jwt.decode", "jwt", "decode", "verify", "PyJWT", _jwt_decode),
        Spec("cryptography.Fernet.encrypt", "cryptography.fernet", "Fernet.encrypt", "encrypt", "cryptography", _fernet),
        Spec("cryptography.Fernet.decrypt", "cryptography.fernet", "Fernet.decrypt", "decrypt", "cryptography", _fernet),
        Spec("cryptography.PBKDF2HMAC.derive", "cryptography.hazmat.primitives.kdf.pbkdf2", "PBKDF2HMAC.derive", "kdf",
             "cryptography", _crypto_pbkdf2, slow=True),
        Spec("cryptography.Scrypt.derive", "cryptography.hazmat.primitives.kdf.scrypt", "Scrypt.derive", "kdf",
             "cryptography", _crypto_scrypt, slow=True),
        Spec("cryptography.AESGCM.encrypt", "cryptography.hazmat.primitives.ciphers.aead", "AESGCM.encrypt", "encrypt",
             "cryptography", _aead("AES-GCM")),
        Spec("cryptography.AESGCM.decrypt", "cryptography.hazmat.primitives.ciphers.aead", "AESGCM.decrypt", "decrypt",
             "cryptography", _aead("AES-GCM")),
        Spec("cryptography.ChaCha20Poly1305.encrypt", "cryptography.hazmat.primitives.ciphers.aead",
             "ChaCha20Poly1305.encrypt", "encrypt", "cryptography", _aead("ChaCha20-Poly1305")),
        Spec("cryptography.ChaCha20Poly1305.decrypt", "cryptography.hazmat.primitives.ciphers.aead",
             "ChaCha20Poly1305.decrypt", "decrypt", "cryptography", _aead("ChaCha20-Poly1305")),
    ]
    return s


SPECS = _specs()

# ----------------------------------------------------------------------------- wrappers

WRAPPED = "__vayunx_original__"

# Threads currently inside an "outer" hooked call (one that may call other hooked functions: HMAC,
# KDFs, passlib, JWT...). Calls made inside it are part of the outer call and are not recorded again.
# Leaf hooks (plain hash constructors) only test whether this dict is empty: ~20 ns, versus ~85 ns per
# threading.local access (measured on an i5-8400H). One key per thread, so setitem/pop from different
# threads never collide.
_inside: dict[int, int] = {}
_OUTER_FAST = {"hmac.new", "hmac.digest", "jwt.encode", "jwt.decode", "cryptography.Fernet.encrypt",
               "cryptography.Fernet.decrypt"}

# The innermost vayunx.span()/measure() name. Crypto series recorded inside it carry it as `vayunx.scope`,
# so the compare view can match "the hashing done in login" across variants, apart from unrelated hashing.
SCOPE: ContextVar[str | None] = ContextVar("vayunx_scope", default=None)

# Histogram series keys are interned to small ints so the hot path never hashes nested tuples.
_key_ids: dict[tuple, int] = {}
KEY_ATTRS: list[tuple] = []  # id -> (("crypto.operation", ..), ("crypto.algorithm", ..), ...)
_key_lock = threading.Lock()


def key_id(spec: Spec, algorithm: str, params: str | None, scope: str | None = None) -> int:
    attrs = (("crypto.operation", spec.operation), ("crypto.algorithm", algorithm), ("crypto.params", params),
             ("crypto.library", spec.library), ("vayunx.scope", scope))
    with _key_lock:
        kid = _key_ids.get(attrs)
        if kid is None:
            kid = _key_ids[attrs] = len(KEY_ATTRS)
            KEY_ATTRS.append(attrs)
    return kid


def _unwrap(value):
    return getattr(value, WRAPPED, value)


def _report(st, spec, args, kwargs, t0, t1, c0, err) -> None:
    try:
        st.on_call(spec, args, kwargs, t0, t1, c0, err)
    except Exception:
        try:
            st.internal_error()
        except Exception:
            pass


def make_wrapper(spec: Spec, orig: Callable, cell: list) -> Callable:
    """`cell[0]` is the active state (or None). It is read per call, so a shutdown between calls turns
    every wrapper into a pass-through immediately, even for references the app copied."""
    wrapper = _outer_wrapper(spec, orig, cell) if spec.slow else _fast_wrapper(spec, orig, cell)
    setattr(wrapper, WRAPPED, orig)
    return wrapper


def _fast_wrapper(spec: Spec, orig: Callable, cell: list) -> Callable:
    """Microsecond-scale primitives (hashes, HMAC, AEAD, signatures): the histogram update is inlined,
    and only hooks that can contain other hooked calls pay for the nesting guard."""
    fixed_kid = key_id(spec, *spec.fixed) if spec.fixed is not None else None
    keyfn = spec.keyfn or (lambda a, k: spec.describe(a, k)[:2])
    keys = spec.keys
    outer = spec.name in _OUTER_FAST
    unwrap_digest = spec.name in ("hmac.new", "hmac.digest")
    inside, get_ident, pc = _inside, threading.get_ident, perf_counter_ns
    scope_get = SCOPE.get
    scoped: dict[str, int] = {}  # scope -> series id, for fixed-key hooks

    @functools.wraps(orig)
    def wrapper(*args, **kwargs):
        st = cell[0]
        if st is None or (inside and get_ident() in inside):
            return orig(*args, **kwargs)
        if unwrap_digest:  # keep hmac on its OpenSSL fast path: it only takes builtin digest constructors
            if len(args) > 2:
                if type(args[2]) is FunctionType:
                    args = args[:2] + (_unwrap(args[2]),) + args[3:]
            elif kwargs:
                for k in ("digestmod", "digest"):
                    if type(kwargs.get(k)) is FunctionType:
                        kwargs[k] = _unwrap(kwargs[k])
        if outer:
            tid = get_ident()
            inside[tid] = 1
        t0 = pc()
        try:
            result = orig(*args, **kwargs)
        except BaseException as exc:
            t1 = pc()
            if outer:
                inside.pop(tid, None)
            _report(st, spec, args, kwargs, t0, t1, 0, exc)
            raise
        t1 = pc()
        if outer:
            inside.pop(tid, None)
        try:
            ns = t1 - t0
            act = st.activity
            act.last_op_ns = t1
            if act.idle:
                act.wake()
            kid = fixed_kid
            scope = scope_get()
            if kid is None:
                ck = keyfn(args, kwargs)
                if scope is not None:
                    ck = (ck[0], ck[1], scope)
                kid = keys.get(ck)
                if kid is None:
                    kid = keys[ck] = key_id(spec, *ck)
            elif scope is not None:
                kid = scoped.get(scope)
                if kid is None:
                    kid = scoped[scope] = key_id(spec, spec.fixed[0], spec.fixed[1], scope)
            q = st.queue
            if len(q) < QUEUE_MAX:
                q.append((kid, ns))  # atomic; folded into histograms by the shipper thread
            else:
                st.ophists.record(kid, ns)  # the shipper is behind: fold inline instead of dropping
            if ns >= st.slow_ns or st.sample_every:
                st.on_slow_or_sampled(spec, args, kwargs, ns, 0)
        except Exception:
            try:
                st.internal_error()
            except Exception:
                pass
        return result

    return wrapper


def _outer_wrapper(spec: Spec, orig: Callable, cell: list) -> Callable:
    """Password hashing and KDFs (milliseconds): CPU time on every call; nested hooked calls ignored."""
    inside, get_ident, pc = _inside, threading.get_ident, perf_counter_ns

    @functools.wraps(orig)
    def wrapper(*args, **kwargs):
        st = cell[0]
        if st is None:
            return orig(*args, **kwargs)
        tid = get_ident()
        if tid in inside:
            return orig(*args, **kwargs)
        inside[tid] = 1
        c0 = thread_cpu_ns()
        t0 = pc()
        err = None
        try:
            return orig(*args, **kwargs)
        except BaseException as exc:
            err = exc
            raise
        finally:
            t1 = pc()
            inside.pop(tid, None)
            _report(st, spec, args, kwargs, t0, t1, c0, err)

    return wrapper


def _dist_version(dist: str | None) -> str:
    if dist is None:
        return f"python {sys.version_info.major}.{sys.version_info.minor}"
    try:
        from importlib.metadata import version
        return f"{dist} {version(dist)}"
    except Exception:
        return dist


class HookManager:
    def __init__(self, cell: list):
        self.cell = cell
        self.status: dict[str, str] = {s.name: "not imported" for s in SPECS}
        self._patched: list[tuple[object, str, object]] = []  # (owner, attr, original)
        self._originals: dict[str, Callable] = {}
        self._lock = threading.RLock()
        self._finder: _PostImportFinder | None = None

    def install(self) -> None:
        with self._lock:
            for mod in ("hashlib", "hmac"):
                __import__(mod)
            waiting = set()
            for spec in SPECS:
                if spec.module in sys.modules:
                    self._install(spec, sys.modules[spec.module])
                else:
                    waiting.add(spec.module)
            if waiting:
                self._finder = _PostImportFinder(waiting, self._on_import)
                sys.meta_path.insert(0, self._finder)

    def _on_import(self, name: str, module) -> None:
        with self._lock:
            if self._finder is None:
                return
            for spec in SPECS:
                if spec.module == name and self.status[spec.name] == "not imported":
                    self._install(spec, module)

    def _install(self, spec: Spec, module) -> None:
        try:
            owner = module
            *parents, attr = spec.path.split(".")
            for p in parents:
                owner = getattr(owner, p)
            orig = owner.__dict__.get(attr) if isinstance(owner, type) else getattr(owner, attr, None)
            if orig is None:
                self.status[spec.name] = "not available in this version"
                return
            if hasattr(orig, WRAPPED):
                self.status[spec.name] = "installed"
                return
            spec.library = _dist_version(spec.dist) if spec.dist else "hashlib" if spec.module == "hashlib" else spec.module
            if spec.dist is None:
                spec.library = f"{spec.module} ({_dist_version(None)})"
            wrapper = make_wrapper(spec, orig, self.cell)
            setattr(owner, attr, wrapper)
            self._patched.append((owner, attr, orig))
            self._originals[spec.name] = orig
            self.status[spec.name] = "installed"
        except (TypeError, AttributeError) as exc:  # e.g. a Rust/C type that refuses new attributes
            self.status[spec.name] = f"unhookable ({type(exc).__name__})"
        except Exception as exc:
            self.status[spec.name] = f"failed ({type(exc).__name__})"

    def original(self, name: str) -> Callable | None:
        return self._originals.get(name)

    def uninstall(self) -> None:
        with self._lock:
            if self._finder is not None:
                try:
                    sys.meta_path.remove(self._finder)
                except ValueError:
                    pass
                self._finder = None
            for owner, attr, orig in reversed(self._patched):
                try:
                    setattr(owner, attr, orig)
                except Exception:
                    pass
            self._patched.clear()
            self._originals.clear()


class _PostImportFinder:
    """Runs `callback(name, module)` right after a watched module finishes executing."""

    def __init__(self, names: set[str], callback):
        self.names, self.callback = names, callback
        self._busy = threading.local()

    def find_spec(self, fullname, path=None, target=None):
        if fullname not in self.names or getattr(self._busy, "on", False):
            return None
        self._busy.on = True
        try:
            spec = importlib.util.find_spec(fullname)
        except Exception:
            return None
        finally:
            self._busy.on = False
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        spec.loader = _NotifyingLoader(spec.loader, fullname, self.callback)
        return spec


class _NotifyingLoader:
    def __init__(self, loader, name: str, callback):
        self._loader, self._name, self._callback = loader, name, callback

    def create_module(self, spec):
        return self._loader.create_module(spec)

    def exec_module(self, module):
        self._loader.exec_module(module)
        try:
            self._callback(self._name, module)
        except Exception:
            pass

    def __getattr__(self, item):  # get_resource_reader, get_source, is_package, ...
        return getattr(self._loader, item)
