from datetime import datetime, timedelta, timezone

from profiler_service.comparison import (
    BASELINE_ONLY, MATCHED, REMEDIATED_ONLY, SampleRec, SpanRec, build_report, build_tree, compare_samples,
    compare_spans, summarize, to_flame_graph,
)

T0 = datetime(2026, 9, 17, 10, 0, 0)


def span(sid, parent, name, dur, cat="general", offset_ms=0.0, attrs=None):
    start = T0 + timedelta(milliseconds=offset_ms)
    return SpanRec(sid, parent, name, cat, start, start + timedelta(milliseconds=dur), dur, attrs or {})


def sample(metric, value, i, unit="MiB", cat="cryptographic"):
    return SampleRec(metric, cat, value, unit, T0 + timedelta(milliseconds=100 * i))


def test_nesting_and_same_path_merge():
    spans = [span("a", None, "login", 10, offset_ms=0), span("a1", "a", "hash_password", 6, "cryptographic", 1),
             span("b", None, "login", 20, offset_ms=50), span("b1", "b", "hash_password", 14, "cryptographic", 51)]
    roots, warnings = build_tree(spans)
    assert warnings == []
    assert [r.name for r in roots] == ["login"]
    login = roots[0]
    assert (login.count, login.total_ms, login.min_ms, login.max_ms) == (2, 30, 10, 20)
    assert login.children[0].path == ("login", "hash_password")
    assert login.children[0].count == 2 and login.children[0].total_ms == 20


def test_unmatched_paths_are_explicit_not_zero():
    base, _ = build_tree([span("r", None, "hash_password", 5), span("c", "r", "md5_hash", 4)])
    rem, _ = build_tree([span("r", None, "hash_password", 50), span("c", "r", "argon2_hash", 49)])
    rows = {tuple(r["path"]): r for r in compare_spans(base, rem)}
    assert rows[("hash_password",)]["status"] == MATCHED
    md5, argon = rows[("hash_password", "md5_hash")], rows[("hash_password", "argon2_hash")]
    assert md5["status"] == BASELINE_ONLY and md5["remediated"] is None and md5["delta_total_ms"] is None
    assert argon["status"] == REMEDIATED_ONLY and argon["baseline"] is None and argon["pct_total"] is None
    assert "remediated only" in argon["notes"][0]


def test_same_name_at_different_position_is_not_matched():
    base, _ = build_tree([span("r", None, "login", 5), span("c", "r", "sanitize_input", 1)])
    rem, _ = build_tree([span("r", None, "login", 5), span("h", "r", "hash_password", 4), span("c", "h", "sanitize_input", 1)])
    statuses = {tuple(r["path"]): r["status"] for r in compare_spans(base, rem)}
    assert statuses[("login", "sanitize_input")] == BASELINE_ONLY
    assert statuses[("login", "hash_password", "sanitize_input")] == REMEDIATED_ONLY


def test_zero_baseline_percent_is_undefined():
    base, _ = build_tree([span("r", None, "noop", 0.0)])
    rem, _ = build_tree([span("r", None, "noop", 1.0)])
    row = compare_spans(base, rem)[0]
    assert row["delta_total_ms"] == 1.0 and row["pct_total"] is None
    assert any("undefined" in n for n in row["notes"])


def test_orphan_and_duration_warnings():
    bad = SpanRec("x", None, "weird", "general", T0, T0 + timedelta(milliseconds=100), 5.0, {})
    _, warnings = build_tree([span("c", "missing-parent", "child", 1), bad])
    assert any("parent_span_id that was never received" in w for w in warnings)
    assert any("inconsistent with end_time - start_time" in w for w in warnings)


def test_flame_graph_shape():
    roots, _ = build_tree([span("a", None, "login", 10), span("a1", "a", "hash", 7, "cryptographic", attrs={"algorithm": "MD5"}),
                           span("b", None, "logout", 3, offset_ms=20)])
    fg = to_flame_graph(roots, "baseline: x")
    assert fg["synthetic_root"] and fg["value"] == 13
    assert [c["name"] for c in fg["children"]] == ["login", "logout"]
    hash_node = fg["children"][0]["children"][0]
    assert hash_node["value"] == 7 and hash_node["category"] == "cryptographic" and hash_node["attributes"] == {"algorithm": ["MD5"]}


def test_sample_deltas_units_and_low_counts():
    base = [sample("memory_mb", v, i) for i, v in enumerate([10, 12, 14])] + [sample("cpu_pct", 5, 0, "%")]
    rem = [sample("memory_mb", v, i) for i, v in enumerate([20, 24, 28])] + [sample("cpu_pct", 50, 0, "percent")]
    rows = {r["metric_name"]: r for r in compare_samples(base, rem)}
    mem = rows["memory_mb"]
    assert mem["baseline"] == {"count": 3, "avg": 12, "min": 10, "max": 14}
    assert mem["delta_max"] == 14 and round(mem["pct_avg"], 6) == 100.0 and mem["notes"] == []
    cpu = rows["cpu_pct"]
    assert cpu["unit"] is None and cpu["delta_avg"] is None
    assert any("units differ" in n for n in cpu["notes"]) and any("indicative only" in n for n in cpu["notes"])


def _rows(base_ms, rem_ms, base_mem, rem_mem):
    b, _ = build_tree([span("r", None, "login", base_ms + 1), span("h", "r", "compute_digest", base_ms, "cryptographic")])
    r, _ = build_tree([span("r", None, "login", rem_ms + 1), span("h", "r", "compute_digest", rem_ms, "cryptographic")])
    bs = [sample("memory_mb", base_mem, i) for i in range(3)] + [sample("cpu_pct", 20.0, i, "%") for i in range(3)]
    rs = [sample("memory_mb", rem_mem, i) for i in range(3)] + [sample("cpu_pct", 90.0, i, "%") for i in range(3)]
    return compare_spans(b, r), compare_samples(bs, rs)


def test_summary_is_built_from_the_data():
    spans, samples = _rows(base_ms=0.123, rem_ms=456.789, base_mem=30.0, rem_mem=97.5)
    text = summarize(spans, samples)
    # per-call values in automatic units, x ratios for large changes, CPU as cores busy
    assert "'login > compute_digest'" in text  # most specific span, not its inclusive parent
    assert "123 µs → 457 ms per call (about 3,700× slower)" in text
    assert "cores busy 0.20 → 0.90 (4.5× higher)" in text
    assert "peak memory 30.0 MiB → 97.5 MiB (3.2× more)" in text
    assert "slower and uses more CPU and more memory than baseline, consistent with a deliberately slow, memory-hard algorithm" in text


def test_summary_zero_cpu_baseline_wording():
    spans, samples = _rows(base_ms=0.5, rem_ms=300.0, base_mem=30.0, rem_mem=90.0)
    for row in samples:
        if row["metric_name"] == "cpu_pct":
            row["baseline"]["avg"], row["pct_avg"] = 0.0, None
    text = summarize(spans, samples)
    assert "cores busy 0.00 → 0.90 (n/a (baseline is 0))" in text
    assert "uses more CPU and more memory" in text and "slower and more memory" not in text


def test_summary_does_not_claim_memory_hardness_without_memory_increase():
    spans, samples = _rows(base_ms=1.0, rem_ms=300.0, base_mem=40.0, rem_mem=40.5)
    text = summarize(spans, samples)
    assert "memory-hard algorithm" not in text and "memory-hardness is not evidenced" in text


def test_report_flags_environment_difference_and_incomplete_run():
    def run(phase, host, completed):
        return {"run_id": phase, "service": "s", "label": phase, "phase": phase, "created_at": "t",
                "completed_at": "t" if completed else None, "metadata": {"hostname": host}}
    rep = build_report(run("baseline", "h1", True), run("remediated", "h2", False),
                       [span("a", None, "x", 1)], [span("a", None, "x", 2)], [], [])
    assert any("environment differs" in w for w in rep["warnings"])
    assert any("never marked complete" in w for w in rep["warnings"])
    assert rep["baseline"]["flame_graph"]["name"] == "baseline: baseline"
