"""Security context per preset (spec G).

Source: OWASP Password Storage Cheat Sheet, as summarised in the build brief and checked 2026-09-19.
RE-CHECK the cheat sheet before shipping; parameters recommended there change over time.
    Argon2id minimum m=19 MiB, t=2, p=1 (equivalents: 46 MiB/t=1, 12 MiB/t=3, 9 MiB/t=4, 7 MiB/t=5, all p=1)
    scrypt N=2^17 (128 MiB), r=8, p=1
    bcrypt work factor >= 10; input limited to 72 bytes
    PBKDF2-HMAC-SHA256 600,000 iterations (the FIPS-friendly choice)
    Fast hashes (MD5, SHA-1, single-pass SHA-256) are not suitable for password storage.
"""

from __future__ import annotations

from vayunx_lab.presets import Preset

REFERENCE = "OWASP Password Storage Cheat Sheet (checked 2026-09-19; re-check before shipping)"

_NOTES = {
    "md5": (False, False, "Fast hash: not suitable for password storage. Fine only for non-security checksums."),
    "sha256": (False, False, "Fast hash: single-pass SHA-256 is not suitable for password storage."),
    "pbkdf2-sha256-600k": (True, True, "Meets the OWASP recommendation for PBKDF2-HMAC-SHA256 (600,000 iterations); "
                                       "the FIPS-friendly choice. Not memory-hard."),
    "bcrypt-10": (True, True, "Meets the OWASP minimum work factor (10). Input is limited to 72 bytes, so longer "
                              "passwords need explicit handling."),
    "bcrypt-12": (True, True, "Above the OWASP minimum work factor (10). Input is limited to 72 bytes, so longer "
                              "passwords need explicit handling."),
    "scrypt-n17": (True, True, "Meets the OWASP recommendation (N=2^17, r=8, p=1; about 128 MiB per hash). Memory-hard."),
    "argon2id-owasp": (True, True, "Exactly the OWASP minimum for Argon2id (m=19 MiB, t=2, p=1). Memory-hard; "
                                   "OWASP's first choice."),
    "argon2id-rfc9106-low": (True, True, "Stronger than the OWASP minimum (m=64 MiB, t=3, p=4; the RFC 9106 "
                                         "low-memory option). Memory-hard; OWASP's first choice."),
}


_FAST = ("md5", "sha1", "sha-1", "sha256", "sha-256", "sha512", "sha-512")
_SLOW = {"argon2id": "Argon2id", "argon2": "Argon2", "bcrypt": "bcrypt", "scrypt": "scrypt", "pbkdf2": "PBKDF2"}


# OWASP minimums as parameter sets (see the module docstring). Argon2id: any of these (memory KiB, passes), p >= 1.
ARGON2ID_MINIMUMS = ((47104, 1), (19456, 2), (12288, 3), (9216, 4), (7168, 5))
BCRYPT_MIN_COST = 10
PBKDF2_SHA256_MIN_ITERATIONS = 600_000
SCRYPT_MIN = {"n": 2 ** 17, "r": 8, "p": 1}


def _params(params: str | None) -> dict[str, int]:
    out = {}
    for part in (params or "").replace(" ", "").split(","):
        k, _, v = part.partition("=")
        if k and v.isdigit():
            out[k.lower()] = int(v)
    return out


def owasp_check(algorithm: str | None, params: str | None) -> tuple[bool | None, str]:
    """(meets the OWASP password-storage minimum?, why). None = cannot tell (unknown algorithm or parameters)."""
    name = (algorithm or "").strip().lower().replace("_", "-")
    if not name:
        return None, "No algorithm recorded."
    if any(name == f or name.startswith(f + " ") for f in _FAST):
        return False, "Fast hash: no parameters make it suitable for password storage."
    p = _params(params)
    if name.startswith("argon2id"):
        if not {"m", "t"} <= set(p):
            return None, "Argon2id parameters were not recorded."
        ok = any(p["m"] >= m and p["t"] >= t for m, t in ARGON2ID_MINIMUMS) and p.get("p", 1) >= 1
        return ok, (f"m={p['m']} KiB, t={p['t']} {'meets' if ok else 'is below'} the OWASP minimum "
                    "(m=19456 KiB with t=2, or an equivalent: 47104/1, 12288/3, 9216/4, 7168/5).")
    if name.startswith("bcrypt"):
        if "cost" not in p:
            return None, "The bcrypt cost was not recorded."
        ok = p["cost"] >= BCRYPT_MIN_COST
        return ok, f"Cost {p['cost']} {'meets' if ok else 'is below'} the OWASP minimum work factor of {BCRYPT_MIN_COST}."
    if name.startswith("pbkdf2"):
        if name != "pbkdf2-hmac-sha256":
            return None, f"{algorithm}: only PBKDF2-HMAC-SHA256 is checked here (OWASP: 600,000 iterations)."
        it = p.get("i", p.get("iterations"))
        if it is None:
            return None, "The PBKDF2 iteration count was not recorded."
        ok = it >= PBKDF2_SHA256_MIN_ITERATIONS
        return ok, f"{it:,} iterations {'meets' if ok else 'is below'} the OWASP recommendation of 600,000."
    if name.startswith("scrypt"):
        if not {"n", "r", "p"} <= set(p):
            return None, "scrypt parameters were not recorded."
        ok = p["n"] >= SCRYPT_MIN["n"] and p["r"] >= SCRYPT_MIN["r"] and p["p"] >= SCRYPT_MIN["p"]
        return ok, f"N={p['n']}, r={p['r']}, p={p['p']} {'meets' if ok else 'is below'} the OWASP minimum (N=2^17, r=8, p=1)."
    return None, f"{algorithm} is not one of the algorithms checked here."


def note_for_algorithm(algorithm: str | None, params: str | None = None) -> dict | None:
    """Note for app data, where the algorithm name and (when the SDK could read them) its parameters are known.
    Password hashes are checked against the OWASP minimums when their parameters were recorded."""
    if not algorithm:
        return None
    name = algorithm.strip().lower().replace("_", "-")
    if any(name == f or name.startswith(f + " ") for f in _FAST):
        return {"algorithm": algorithm, "params": params or "", "safe_for_passwords": False, "meets_owasp_minimum": False,
                "summary": "Fast hash: not suitable for password storage.", "reference": REFERENCE}
    for key, label in _SLOW.items():
        if name.startswith(key):
            meets, why = owasp_check(algorithm, params)
            prefix = f"{label} is a password hash. "
            return {"algorithm": label, "params": params or "", "safe_for_passwords": True, "meets_owasp_minimum": meets,
                    "summary": prefix + (why if meets is not None else why + " Its parameters were not checked against the "
                                         "OWASP minimums."), "reference": REFERENCE}
    return None


def security_note(preset: Preset) -> dict:
    safe, meets_minimum, summary = _NOTES[preset.base_id]
    return {"algorithm": preset.algorithm, "params": preset.params, "safe_for_passwords": safe,
            "meets_owasp_minimum": meets_minimum, "summary": summary, "reference": REFERENCE}
