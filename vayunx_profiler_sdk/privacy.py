"""Attribute scrubbing: the SDK records sizes, parameters and timings - never secrets.

Dropped (silently, counted by the client):
* byte-like values (bytes, bytearray, memoryview) - raw key/salt/hash material
* values that are not str/int/float/bool/None, and strings longer than 256 characters
* keys that look sensitive (password, secret, key, salt, token, hash, digest, plaintext, ...),
  except the explicit safe keys below (e.g. crypto.key_bits is a size, not a key).
"""

from __future__ import annotations

import re

SAFE_KEYS = frozenset({
    "crypto.algorithm", "crypto.operation", "crypto.params", "crypto.library", "crypto.library_version",
    "crypto.input_bytes", "crypto.output_bytes", "crypto.key_bits", "crypto.sync",
})
SENSITIVE = re.compile(r"pass(word|wd)?|pwd|secret|key|salt|token|hash|digest|plain|cipher|credential|nonce|pepper|otp|seed",
                       re.IGNORECASE)
MAX_STRING = 256


def _allowed_value(value) -> bool:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return False
    if isinstance(value, str):
        return len(value) <= MAX_STRING
    return value is None or isinstance(value, (bool, int, float))


def clean_attributes(attributes: dict | None) -> tuple[dict, int]:
    """Return (safe attributes, number dropped). Never raises."""
    if not attributes:
        return {}, 0
    clean, dropped = {}, 0
    try:
        items = list(attributes.items())
    except Exception:
        return {}, 1
    for key, value in items:
        if not isinstance(key, str) or not key or len(key) > 64:
            dropped += 1
        elif key not in SAFE_KEYS and SENSITIVE.search(key):
            dropped += 1
        elif not _allowed_value(value):
            dropped += 1
        else:
            clean[key] = value
    return clean, dropped
