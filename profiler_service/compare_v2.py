"""Compare engine v2 (spec D): N >= 2 variants, statistics across trials, weak-data flags,
capacity estimates, security context and a verdict built only from computed rows.

Statistics
* Per trial: per-call p50/p95/p99, mean and IQR from the trial's latency histogram (log2x8-ns).
* Per variant, across trials: the MEDIAN of the trial values; spread = min-max of trial medians plus
  a seeded bootstrap 95% CI of the median.
* Significance vs the reference is conservative: "not significant" whenever the min-max ranges of
  trial medians overlap.

Derived values (all labelled estimates): CPU-seconds per operation, operations per second per core,
cores needed at a given rate (rate x CPU-s/op), RAM in flight (rate x latency x memory per op).

Nothing here falls back to example figures: with no data, fields are None.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field

from profiler_service.formatting import fmt_bytes, fmt_change, fmt_duration_ns
from vayunx_profiler_sdk.histogram import LatencyHistogram

SCHEMA_VERSION = "2"
MIN_TRIALS = 5
MIN_OPS_PER_TRIAL = 30
UNSTABLE_SPREAD = 0.20  # (max - min) / median of trial medians
TIMER_OVERHEAD_SHARE = 0.10
MIN_TRIAL_WALL_S = 0.3  # ~3 ticks of the Lab's 100 ms machine sampler
BOOTSTRAP_RESAMPLES = 2000
DEFAULT_RATE_PER_S = 100.0


@dataclass
class TrialInput:
    variant_key: str
    variant_label: str
    trial_index: int
    histogram: dict
    wall_s: float | None = None
    cpu_s_per_op: float | None = None
    cores_busy: float | None = None
    peak_rss_bytes: int | None = None
    rss_before_bytes: int | None = None
    noisy: bool = False
    timer_overhead_ns: float | None = None
    concurrency: int = 1
    family: str | None = None  # "password-hash" | "fast-hash" | None
    security: dict | None = None
    peak_rss_approximate: bool = False
    extra: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------- helpers


def _median(xs):
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _bootstrap_ci(values: list[float], seed: int = 0) -> list[float] | None:
    if not values:
        return None
    if len(values) == 1:
        return [values[0], values[0]]
    rng = random.Random(seed)
    meds = sorted(statistics.median(rng.choices(values, k=len(values))) for _ in range(BOOTSTRAP_RESAMPLES))
    return [meds[int(0.025 * (len(meds) - 1))], meds[int(0.975 * (len(meds) - 1))]]


def _flag(code: str, message: str, affects: list[str]) -> dict:
    return {"code": code, "message": message, "affects": affects}


# ----------------------------------------------------------------------------- per variant


def aggregate_variant(trials: list[TrialInput]) -> dict:
    first = trials[0]
    per_trial = []
    for t in trials:
        h = LatencyHistogram.from_dict(t.histogram)
        if not h.count:
            continue
        p25, p75 = h.percentile(25), h.percentile(75)
        per_trial.append({"trial_index": t.trial_index, "ops": h.count, "p50": h.percentile(50), "p95": h.percentile(95),
                          "p99": h.percentile(99), "mean": h.sum_ns / h.count, "iqr": (p75 - p25) if p25 is not None else None,
                          "wall_s": t.wall_s, "noisy": t.noisy})
    medians = [p["p50"] for p in per_trial]
    median_ns = _median(medians)
    mem_per_op = _median([(t.peak_rss_bytes - t.rss_before_bytes) / max(1, t.concurrency)
                          for t in trials if t.peak_rss_bytes is not None and t.rss_before_bytes is not None])
    cpu_s_per_op = _median([t.cpu_s_per_op for t in trials])
    row = {
        "key": first.variant_key, "label": first.variant_label, "family": first.family, "security": first.security,
        "trials": len(per_trial),
        "time_per_call": {
            "median_ns": median_ns, "p50_ns": median_ns,
            "p95_ns": _median([p["p95"] for p in per_trial]), "p99_ns": _median([p["p99"] for p in per_trial]),
            "mean_ns": _median([p["mean"] for p in per_trial]), "iqr_ns": _median([p["iqr"] for p in per_trial]),
            "trial_medians_ns": medians, "range_ns": [min(medians), max(medians)] if medians else None,
            "ci95_ns": _bootstrap_ci(medians),
        },
        "cpu": {"cpu_s_per_op": cpu_s_per_op, "ops_per_s_per_core": (1 / cpu_s_per_op) if cpu_s_per_op else None,
                "cores_busy": _median([t.cores_busy for t in trials])},
        "memory": {"peak_rss_bytes": _median([t.peak_rss_bytes for t in trials]), "mem_per_op_bytes": mem_per_op,
                   "approximate": any(t.peak_rss_approximate for t in trials)},
        "throughput": {"ops_per_s": _median([p["ops"] / p["wall_s"] for p in per_trial if p["wall_s"]])},
        "per_trial": per_trial,
    }
    row["flags"] = _flags(row, trials, per_trial)
    return row


def _flags(row: dict, trials: list[TrialInput], per_trial: list[dict]) -> list[dict]:
    flags = []
    n = len(per_trial)
    tpc = row["time_per_call"]
    if n < MIN_TRIALS:
        flags.append(_flag("few_trials", f"Only {n} trial(s); at least {MIN_TRIALS} are needed to trust the spread.",
                           ["time_per_call", "cores_busy", "peak_ram"]))
    min_ops = min((p["ops"] for p in per_trial), default=0)
    if per_trial and min_ops < MIN_OPS_PER_TRIAL:
        flags.append(_flag("few_ops", f"Only {min_ops} operations in a trial; p95 and p99 are unreliable below "
                                      f"{MIN_OPS_PER_TRIAL}.", ["time_per_call"]))
    noisy = sum(1 for p in per_trial if p["noisy"])
    if noisy:
        flags.append(_flag("noisy_machine", f"{noisy} of {n} trials ran while other work was using the machine.",
                           ["time_per_call", "cores_busy"]))
    if tpc["median_ns"] and tpc["range_ns"] and n >= 2:
        spread = (tpc["range_ns"][1] - tpc["range_ns"][0]) / tpc["median_ns"]
        if spread > UNSTABLE_SPREAD:
            flags.append(_flag("unstable", f"Trial medians vary by {spread * 100:.0f}% of the median; the time per call "
                                           "is not stable between trials.", ["time_per_call"]))
    overhead = _median([t.timer_overhead_ns for t in trials])
    if overhead and tpc["median_ns"] and overhead / tpc["median_ns"] > TIMER_OVERHEAD_SHARE:
        flags.append(_flag("timer_overhead", f"Measurement overhead (about {fmt_duration_ns(overhead)} per call) is "
                                             f"{overhead / tpc['median_ns'] * 100:.0f}% of the time per call, so the real "
                                             "cost is somewhat lower.", ["time_per_call"]))
    walls = [p["wall_s"] for p in per_trial if p["wall_s"] is not None]
    if walls and min(walls) < MIN_TRIAL_WALL_S:
        flags.append(_flag("short_run", f"A trial lasted {min(walls):.2f} s, too short for more than a few machine "
                                        "samples.", ["cores_busy", "peak_ram"]))
    if row["memory"]["approximate"]:
        flags.append(_flag("approx_memory", "Peak memory comes from periodic sampling and is approximate.", ["peak_ram"]))
    if any(t.concurrency > 1 for t in trials):
        flags.append(_flag("concurrency_memory", "Several operations ran at once; memory per operation is the peak "
                                                 "divided by the number of workers.", ["peak_ram"]))
    return flags


# ----------------------------------------------------------------------------- comparison


def _vs_reference(row: dict, ref: dict) -> dict:
    a, b = ref["time_per_call"], row["time_per_call"]
    significant = None
    if a["range_ns"] and b["range_ns"]:
        significant = a["range_ns"][1] < b["range_ns"][0] or b["range_ns"][1] < a["range_ns"][0]
    change = fmt_change(a["median_ns"], b["median_ns"])
    if significant is False:
        change += " (not significant)"
    ratio = (b["median_ns"] / a["median_ns"]) if a["median_ns"] and b["median_ns"] else None
    return {"time_ratio": ratio, "time_change": change, "significant": significant,
            "cpu_change": fmt_change(ref["cpu"]["cpu_s_per_op"], row["cpu"]["cpu_s_per_op"], "more CPU", "less CPU"),
            "memory_change": fmt_change(ref["memory"]["peak_rss_bytes"], row["memory"]["peak_rss_bytes"], "more", "less")}


def capacity(row: dict, rate_per_s: float, cores_total: int, reference: dict | None = None) -> dict:
    """Estimate for a sustained rate of operations (e.g. logins per second). Labelled as an estimate."""
    cpu = row["cpu"]["cpu_s_per_op"]
    latency_ns = row["time_per_call"]["median_ns"]
    mem = row["memory"]["mem_per_op_bytes"]
    cores = rate_per_s * cpu if cpu is not None else None
    ref_latency = reference["time_per_call"]["median_ns"] if reference else None
    return {
        "estimate": True, "rate_per_s": rate_per_s, "cores_total": cores_total,
        "cores_needed": cores, "share_of_machine": (cores / cores_total) if cores is not None and cores_total else None,
        "ram_in_flight_bytes": (rate_per_s * latency_ns / 1e9 * mem) if latency_ns and mem is not None else None,
        "latency_ns": latency_ns,
        "added_latency_ns": (latency_ns - ref_latency) if latency_ns is not None and ref_latency is not None else None,
        "formula": "cores = rate x CPU-seconds per op; RAM in flight = rate x latency x memory per op",
    }


def _verdict(rows: list[dict], ref: dict, op_noun: str) -> str:
    parts = []
    for row in rows:
        if row is ref or row["time_per_call"]["median_ns"] is None:
            continue
        parts.append(f"{row['label']} takes {fmt_duration_ns(row['time_per_call']['median_ns'])} per {op_noun}.")
    if ref["time_per_call"]["median_ns"] is not None:
        parts.append(f"{ref['label']} takes {fmt_duration_ns(ref['time_per_call']['median_ns'])}.")
    for row in rows:
        vs = row.get("vs_reference")
        if not vs or vs["time_ratio"] is None:
            continue
        if vs["significant"] is False:
            parts.append(f"{row['label']} and {ref['label']} are not significantly different in these trials.")
        elif vs["time_ratio"] > 1 and row.get("family") == "password-hash":
            parts.append(f"{row['label']} is {vs['time_change']} than {ref['label']}, which is expected: "
                         "password hashes are slow on purpose.")
    if any(row["flags"] for row in rows):
        parts.append("Some numbers are flagged as weak data.")
    return " ".join(parts)


def compare(trials: list[TrialInput], reference_key: str, *, rate_per_s: float = DEFAULT_RATE_PER_S,
            cores_total: int = 8, source: str = "lab", op_noun: str = "hash") -> dict:
    keys: list[str] = []
    grouped: dict[str, list[TrialInput]] = {}
    for t in trials:
        if t.variant_key not in grouped:
            keys.append(t.variant_key)
            grouped[t.variant_key] = []
        grouped[t.variant_key].append(t)
    if reference_key not in grouped:
        raise ValueError(f"reference {reference_key!r} is not one of the variants {keys}")
    if len(keys) < 2:
        raise ValueError("a comparison needs at least two variants")
    order = [reference_key] + [k for k in keys if k != reference_key]
    rows = [aggregate_variant(grouped[k]) for k in order]
    ref = rows[0]
    for row in rows:
        row["is_reference"] = row is ref
        row["vs_reference"] = None if row is ref else _vs_reference(row, ref)
        row["capacity"] = capacity(row, rate_per_s, cores_total, None if row is ref else ref)
    scorecard = [{
        "key": r["key"], "label": r["label"], "is_reference": r["is_reference"],
        "time_per_call": fmt_duration_ns(r["time_per_call"]["median_ns"]),
        "p95": fmt_duration_ns(r["time_per_call"]["p95_ns"]),
        "cores_busy": None if r["cpu"]["cores_busy"] is None else round(r["cpu"]["cores_busy"], 2),
        "peak_ram": fmt_bytes(r["memory"]["peak_rss_bytes"]),
        "safe_for_passwords": (r["security"] or {}).get("safe_for_passwords"),
        "change": "reference" if r["is_reference"] else r["vs_reference"]["time_change"],
        "flags": [f["code"] for f in r["flags"]],
    } for r in rows]
    return {
        "schema_version": SCHEMA_VERSION, "source": source, "reference": reference_key,
        "verdict": _verdict(rows, ref, op_noun), "variants": rows, "scorecard": scorecard,
        "capacity_defaults": {"rate_per_s": rate_per_s, "cores_total": cores_total},
        "method": {"per_call": "median of per-trial medians; spread = min-max of trial medians and a bootstrap 95% CI",
                   "significance": "not significant when the min-max ranges of trial medians overlap",
                   "estimates": "CPU-s/op, ops/s/core, capacity and RAM in flight are estimates"},
    }
