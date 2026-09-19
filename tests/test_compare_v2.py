"""Compare engine v2 (spec D): N variants, trial statistics, weak-data flags, capacity, verdict."""

import re

import pytest

from profiler_service.compare_v2 import TrialInput, capacity, compare
from vayunx_lab.presets import get_preset
from vayunx_lab.security import security_note
from vayunx_profiler_sdk.histogram import LatencyHistogram

MIB = 1024 * 1024


def hist(center_ns: float, n: int = 400, spread: float = 0.05) -> dict:
    h = LatencyHistogram()
    for i in range(n):
        h.record(int(center_ns * (1 - spread + 2 * spread * i / (n - 1))))
    return h.to_dict()


def trials(key, label, centers, *, cpu_s_per_op=None, peak=None, before=30 * MIB, noisy=(), ops=400,
           timer_overhead_ns=50.0, family="password-hash", cores_busy=1.0, preset_id=None):
    sec = security_note(get_preset(preset_id)) if preset_id else None
    out = []
    for i, c in enumerate(centers):
        out.append(TrialInput(variant_key=key, variant_label=label, trial_index=i, histogram=hist(c, n=ops),
                              wall_s=10.0, cpu_s_per_op=cpu_s_per_op if cpu_s_per_op is not None else c / 1e9,
                              cores_busy=cores_busy, peak_rss_bytes=peak or before, rss_before_bytes=before,
                              noisy=i in noisy, timer_overhead_ns=timer_overhead_ns, family=family, security=sec))
    return out


MD5 = dict(key="md5", label="MD5", family="fast-hash", preset_id="md5")
ARGON = dict(key="argon2id-rfc9106-low", label="Argon2id m=64 MiB t=3 p=4", preset_id="argon2id-rfc9106-low")


def md5_trials(**kw):
    return trials(MD5["key"], MD5["label"], kw.pop("centers", [1100, 1080, 1120, 1090, 1110]), family="fast-hash",
                  preset_id="md5", **kw)


def argon_trials(**kw):
    return trials(ARGON["key"], ARGON["label"], kw.pop("centers", [38.2e6, 38.4e6, 37.9e6, 38.6e6, 38.3e6]),
                  cpu_s_per_op=kw.pop("cpu_s_per_op", 0.13), peak=kw.pop("peak", 98 * MIB), cores_busy=3.4,
                  preset_id="argon2id-rfc9106-low", **kw)


def variant(result, key):
    return next(v for v in result["variants"] if v["key"] == key)


def test_per_call_statistics_across_trials():
    r = compare(md5_trials() + argon_trials(), reference_key="md5")
    a = variant(r, "argon2id-rfc9106-low")["time_per_call"]
    assert a["median_ns"] == pytest.approx(38.3e6, rel=0.07)  # median of trial medians (histogram ±6.25%)
    assert a["p50_ns"] <= a["p95_ns"] <= a["p99_ns"]
    assert len(a["trial_medians_ns"]) == 5 and a["range_ns"][0] <= a["median_ns"] <= a["range_ns"][1]
    assert a["ci95_ns"][0] <= a["median_ns"] <= a["ci95_ns"][1]
    assert r["reference"] == "md5" and variant(r, "md5")["is_reference"] is True


def test_ratio_and_significance_vs_reference():
    r = compare(md5_trials() + argon_trials(), reference_key="md5")
    vs = variant(r, "argon2id-rfc9106-low")["vs_reference"]
    assert vs["time_ratio"] == pytest.approx(38.3e6 / 1100, rel=0.1)
    assert vs["time_change"].startswith("about ") and vs["time_change"].endswith("× slower")
    assert vs["significant"] is True
    assert variant(r, "md5")["vs_reference"] is None


def test_overlapping_trials_are_not_significant():
    other = trials("sha256", "SHA-256", [1105, 1095, 1115, 1085, 1100], family="fast-hash", preset_id="sha256")
    r = compare(md5_trials() + other, reference_key="md5")
    vs = variant(r, "sha256")["vs_reference"]
    assert vs["significant"] is False and "not significant" in vs["time_change"]


def test_weak_data_flags_name_the_numbers_they_affect():
    few = argon_trials(centers=[38e6, 39e6, 38.5e6])
    r = compare(md5_trials() + few, reference_key="md5")
    codes = {f["code"]: f for f in variant(r, "argon2id-rfc9106-low")["flags"]}
    assert "few_trials" in codes and "time_per_call" in codes["few_trials"]["affects"]

    noisy = argon_trials(noisy=(1, 3))
    codes = {f["code"] for f in variant(compare(md5_trials() + noisy, "md5"), "argon2id-rfc9106-low")["flags"]}
    assert "noisy_machine" in codes

    thin = argon_trials(ops=12)
    codes = {f["code"] for f in variant(compare(md5_trials() + thin, "md5"), "argon2id-rfc9106-low")["flags"]}
    assert "few_ops" in codes

    unstable = argon_trials(centers=[30e6, 45e6, 38e6, 52e6, 36e6])
    codes = {f["code"] for f in variant(compare(md5_trials() + unstable, "md5"), "argon2id-rfc9106-low")["flags"]}
    assert "unstable" in codes

    heavy_timer = md5_trials(timer_overhead_ns=300.0)
    codes = {f["code"]: f for f in variant(compare(heavy_timer + argon_trials(), "md5"), "md5")["flags"]}
    assert "timer_overhead" in codes and "time_per_call" in codes["timer_overhead"]["affects"]

    clean = variant(compare(md5_trials() + argon_trials(), "md5"), "argon2id-rfc9106-low")["flags"]
    assert clean == []


def test_capacity_estimate():
    r = compare(md5_trials() + argon_trials(), reference_key="md5", rate_per_s=100, cores_total=8)
    a = variant(r, "argon2id-rfc9106-low")
    cap = capacity(a, rate_per_s=100, cores_total=8, reference=variant(r, "md5"))
    assert cap["cores_needed"] == pytest.approx(13.0)  # 100/s x 0.13 CPU-s per op
    assert cap["share_of_machine"] == pytest.approx(13.0 / 8)
    assert cap["ram_in_flight_bytes"] == pytest.approx(100 * (a["time_per_call"]["median_ns"] / 1e9) * a["memory"]["mem_per_op_bytes"])
    assert cap["added_latency_ns"] == pytest.approx(a["time_per_call"]["median_ns"] - variant(r, "md5")["time_per_call"]["median_ns"])
    assert cap["estimate"] is True and a["capacity"]["rate_per_s"] == 100


def test_verdict_is_built_from_rows_and_never_calls_slower_worse():
    r = compare(md5_trials() + argon_trials(), reference_key="md5")
    v = r["verdict"]
    assert "Argon2id m=64 MiB t=3 p=4 takes" in v and "per hash" in v
    # 1,100 ns synthetic trials, read back from histogram buckets (±6.25%)
    assert re.search(r"MD5 takes 1\.\d\d µs", v)
    assert "slow on purpose" in v
    for word in ("worse", "bad", "regress", "degrad"):
        assert word not in v.lower()


def test_security_notes_are_attached_per_variant():
    r = compare(md5_trials() + argon_trials(), reference_key="md5")
    assert variant(r, "md5")["security"]["safe_for_passwords"] is False
    assert variant(r, "argon2id-rfc9106-low")["security"]["safe_for_passwords"] is True


def test_three_variants_and_any_reference():
    bc = trials("bcrypt-10", "bcrypt cost 10", [56e6, 57e6, 55e6, 56.5e6, 56.2e6], cpu_s_per_op=0.056, preset_id="bcrypt-10")
    r = compare(md5_trials() + argon_trials() + bc, reference_key="bcrypt-10")
    assert [v["key"] for v in r["variants"]][0] == "bcrypt-10"  # reference first
    assert variant(r, "bcrypt-10")["vs_reference"] is None
    assert variant(r, "md5")["vs_reference"]["time_change"].endswith("faster")
    assert len(r["scorecard"]) == 3


def test_exact_percentiles_are_preferred_over_histogram_buckets():
    ex = argon_trials()
    for i, t in enumerate(ex):
        t.exact_percentiles = {"p25_ns": 37_000_000, "p50_ns": 38_000_000 + i, "p75_ns": 39_000_000,
                               "p95_ns": 41_000_000, "p99_ns": 42_000_000}
    a = variant(compare(md5_trials() + ex, "md5"), "argon2id-rfc9106-low")["time_per_call"]
    assert a["median_ns"] == 38_000_002 and a["p95_ns"] == 41_000_000 and a["percentile_method"] == "exact"
    assert variant(compare(md5_trials() + argon_trials(), "md5"), "md5")["time_per_call"]["percentile_method"].startswith("histogram")


def test_memory_per_op_uses_peak_minus_baseline():
    r = compare(md5_trials() + argon_trials(), reference_key="md5")
    mem = variant(r, "argon2id-rfc9106-low")["memory"]
    assert mem["peak_rss_bytes"] == 98 * MIB and mem["mem_per_op_bytes"] == 68 * MIB and mem["approximate"] is False


def test_verdict_between_two_password_hashes_states_the_difference_plainly():
    """Same algorithm, two runtimes: a 2x difference is a finding, not 'slow on purpose'."""
    py = trials("argon2id-owasp", "Argon2id (Python)", [31.8e6, 31.9e6, 32.0e6, 31.7e6, 31.9e6], preset_id="argon2id-owasp")
    node = trials("argon2id-owasp@node", "Argon2id (Node.js)", [71.4e6, 70.9e6, 72.0e6, 71.1e6, 71.6e6],
                  preset_id="argon2id-owasp")
    v = compare(py + node, reference_key="argon2id-owasp")["verdict"]
    assert "slow on purpose" not in v
    assert re.search(r"Argon2id \(Node\.js\) is 2\.\d× slower than Argon2id \(Python\)\.", v)
    v = compare(py + node, reference_key="argon2id-owasp@node")["verdict"]
    assert re.search(r"Argon2id \(Python\) is 2\.\d× faster than Argon2id \(Node\.js\)\.", v)
