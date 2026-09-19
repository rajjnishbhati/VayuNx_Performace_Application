"""The SDK must never ship passwords, keys, salts, plaintexts, hashes or tokens."""

import hashlib
import json
import os

from conftest import RecordingTransport
from vayunx_profiler_sdk import ProfilerClient
from vayunx_profiler_sdk.privacy import clean_attributes


def test_sensitive_attribute_keys_and_bytes_values_are_dropped():
    attrs, dropped = clean_attributes({
        "crypto.algorithm": "Argon2id", "crypto.params": "m=65536,t=3,p=4", "crypto.input_bytes": 28, "crypto.key_bits": 256,
        "password": "hunter2", "user_password": "x", "salt": "abcd", "api_key": "k", "token": "t", "digest": "d",
        "hash_value": "h", "plaintext": "p", "secret": "s", "raw": b"\x00\x01", "blob": bytearray(b"x"),
    })
    assert attrs == {"crypto.algorithm": "Argon2id", "crypto.params": "m=65536,t=3,p=4", "crypto.input_bytes": 28, "crypto.key_bits": 256}
    assert dropped == 11


def test_full_flow_payloads_contain_no_secret_material(fast_opts):
    password = "correct horse battery staple #7"
    salt = os.urandom(16)
    digest = hashlib.sha256(salt + password.encode()).digest()
    t = RecordingTransport()
    c = ProfilerClient("http://unused", "privacy-test", transport=t, **fast_opts)
    with c.run(label="p", phase="baseline", metadata={"password": password, "note": "ok"}) as run:
        c.start_sampling(interval_ms=10, metrics=["memory_mb"])
        with run.span("hash_password", category="cryptographic",
                      attributes={"crypto.algorithm": "SHA-256", "password": password, "salt": salt.hex(), "digest": digest.hex()}):
            op = run.op("compute_digest", attributes={"crypto.input_bytes": len(password), "salt": salt})
            with op:
                hashlib.sha256(salt + password.encode()).digest()
        c.stop_sampling()
    wire = json.dumps([payload for *_, payload in t.calls if payload is not None])
    for secret in (password, salt.hex(), digest.hex(), salt.decode("latin-1")):
        assert secret not in wire
    assert "SHA-256" in wire and c.stats()["dropped_attributes"] >= 4
