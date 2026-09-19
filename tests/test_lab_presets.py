import pytest

from vayunx_lab.presets import PRESETS, PresetError, get_preset
from vayunx_lab.security import security_note
from vayunx_lab.worker import peak_rss_bytes_from_rusage

EXPECTED = {
    "md5": ("MD5", False), "sha256": ("SHA-256", False),
    "pbkdf2-sha256-600k": ("PBKDF2-HMAC-SHA256", True),
    "bcrypt-10": ("bcrypt", True), "bcrypt-12": ("bcrypt", True),
    "scrypt-n17": ("scrypt", True),
    "argon2id-owasp": ("Argon2id", True), "argon2id-rfc9106-low": ("Argon2id", True),
}


def test_all_spec_presets_exist_with_canonical_params():
    assert set(PRESETS) == set(EXPECTED)
    assert get_preset("argon2id-rfc9106-low").params == "m=65536,t=3,p=4"
    assert get_preset("argon2id-owasp").params == "m=19456,t=2,p=1"
    assert get_preset("scrypt-n17").params == "N=131072,r=8,p=1"
    assert get_preset("pbkdf2-sha256-600k").params == "iterations=600000"
    assert get_preset("bcrypt-12").params == "cost=12"


@pytest.mark.parametrize("preset_id", sorted(EXPECTED))
def test_each_preset_performs_one_real_operation(preset_id):
    preset = get_preset(preset_id)
    op = preset.make_op()
    out1, out2 = op(), op()
    assert isinstance(out1, (bytes, str)) and len(out1) >= 16
    if preset.salted:
        assert out1 != out2, "salted presets must use a fresh salt per operation"
    else:
        assert out1 == out2


def test_unknown_preset_is_rejected():
    with pytest.raises(PresetError):
        get_preset("rm -rf /")


@pytest.mark.parametrize("preset_id, algorithm, safe", [(k, *v) for k, v in EXPECTED.items()])
def test_security_notes_follow_the_owasp_reference(preset_id, algorithm, safe):
    note = security_note(get_preset(preset_id))
    assert note["algorithm"] == algorithm
    assert note["safe_for_passwords"] is safe
    assert note["reference"].startswith("OWASP Password Storage Cheat Sheet")
    assert note["summary"]


def test_security_note_specifics():
    assert "72 bytes" in security_note(get_preset("bcrypt-10"))["summary"]
    assert "FIPS" in security_note(get_preset("pbkdf2-sha256-600k"))["summary"]
    assert "not suitable" in security_note(get_preset("md5"))["summary"]
    assert "minimum" in security_note(get_preset("argon2id-owasp"))["summary"]


@pytest.mark.parametrize("platform, raw, expected", [
    ("linux", 100_000, 100_000 * 1024),   # ru_maxrss in KiB on Linux
    ("darwin", 100_000, 100_000),         # bytes on macOS
])
def test_peak_rss_units_by_platform(platform, raw, expected):
    assert peak_rss_bytes_from_rusage(raw, platform) == expected
