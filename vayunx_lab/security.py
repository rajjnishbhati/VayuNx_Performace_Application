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


def note_for_algorithm(algorithm: str | None, params: str | None = None) -> dict | None:
    """Best-effort note for app data, where only an algorithm name (and maybe params) is known.
    Parameters are NOT checked against the OWASP minimums here; the note says so."""
    if not algorithm:
        return None
    name = algorithm.strip().lower().replace("_", "-")
    if any(name == f or name.startswith(f + " ") for f in _FAST):
        return {"algorithm": algorithm, "params": params or "", "safe_for_passwords": False, "meets_owasp_minimum": False,
                "summary": "Fast hash: not suitable for password storage.", "reference": REFERENCE}
    for key, label in _SLOW.items():
        if name.startswith(key):
            return {"algorithm": label, "params": params or "", "safe_for_passwords": True, "meets_owasp_minimum": None,
                    "summary": f"{label} is a password hash. Its parameters were not checked against the OWASP "
                               "minimums for this app data.", "reference": REFERENCE}
    return None


def security_note(preset: Preset) -> dict:
    safe, meets_minimum, summary = _NOTES[preset.id]
    return {"algorithm": preset.algorithm, "params": preset.params, "safe_for_passwords": safe,
            "meets_owasp_minimum": meets_minimum, "summary": summary, "reference": REFERENCE}
