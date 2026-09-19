"""Baseline vs remediated comparison. Pure functions - no database, no HTTP.

Span matching
-------------
Span IDs differ between runs, so spans are matched by *name + position in the tree*:
the path of span names from a root down to the span (e.g. login > hash_password > compute_digest).
Sibling spans with the same name under the same path are aggregated into one node
(count, total, mean, min, max) - the usual flame-graph merge. A path present in only one
run is reported explicitly as baseline_only / remediated_only; it is never defaulted to 0.

Nothing in this module contains or falls back to example numbers: every figure in the
report, including the summary sentence, is computed from the spans/samples passed in.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from profiler_service import WIRE_SCHEMA_VERSION
from profiler_service.config import (
    DURATION_TOLERANCE_MS, DURATION_TOLERANCE_REL, MATERIAL_CHANGE_PCT, MIN_SAMPLES_PER_METRIC,
)

MATCHED, BASELINE_ONLY, REMEDIATED_ONLY = "matched", "baseline_only", "remediated_only"


@dataclass(frozen=True)
class SpanRec:
    span_id: str
    parent_span_id: str | None
    span_name: str
    category: str
    start_time: datetime
    end_time: datetime
    duration_ms: float
    attributes: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SampleRec:
    metric_name: str
    category: str
    value: float
    unit: str
    timestamp: datetime


@dataclass
class AggNode:
    name: str
    path: tuple[str, ...]
    count: int = 0
    total_ms: float = 0.0
    min_ms: float = float("inf")
    max_ms: float = 0.0
    categories: set = field(default_factory=set)
    attributes: dict = field(default_factory=lambda: defaultdict(set))
    children: list["AggNode"] = field(default_factory=list)

    @property
    def mean_ms(self) -> float:
        return self.total_ms / self.count if self.count else 0.0

    def stats(self) -> dict:
        return {"count": self.count, "total_ms": self.total_ms, "mean_ms": self.mean_ms,
                "min_ms": self.min_ms, "max_ms": self.max_ms}


# ----------------------------------------------------------------------------- trees


def build_tree(spans: list[SpanRec]) -> tuple[list[AggNode], list[str]]:
    """Return merged root nodes plus data-quality warnings for one run."""
    warnings: list[str] = []
    by_id = {s.span_id: s for s in spans}
    children: dict[str, list[SpanRec]] = defaultdict(list)
    roots: list[SpanRec] = []
    orphans: list[str] = []
    for s in spans:
        if s.parent_span_id is None:
            roots.append(s)
        elif s.parent_span_id in by_id:
            children[s.parent_span_id].append(s)
        else:
            roots.append(s)
            orphans.append(s.span_id)
    if orphans:
        warnings.append(f"{len(orphans)} span(s) reference a parent_span_id that was never received; shown as roots "
                        f"(e.g. {', '.join(orphans[:5])}).")

    reachable = set()
    stack = [s.span_id for s in roots]
    while stack:
        sid = stack.pop()
        if sid in reachable:
            continue
        reachable.add(sid)
        stack.extend(c.span_id for c in children.get(sid, []))
    unreachable = len(spans) - len(reachable)
    if unreachable:
        warnings.append(f"{unreachable} span(s) are unreachable from any root (parent cycle); excluded from the tree.")

    inconsistent = 0
    for s in spans:
        wall_ms = (s.end_time - s.start_time).total_seconds() * 1000.0
        if abs(wall_ms - s.duration_ms) > max(DURATION_TOLERANCE_MS, DURATION_TOLERANCE_REL * s.duration_ms):
            inconsistent += 1
    if inconsistent:
        warnings.append(f"{inconsistent} span(s) have duration_ms inconsistent with end_time - start_time; durations use duration_ms.")

    merged = _merge(roots, (), children)
    overflow = _count_child_overflow(merged)
    if overflow:
        warnings.append(f"{overflow} node(s) have children whose total exceeds the parent's own total (overlapping or concurrent "
                        "child spans); flame graph widths for those nodes are not to scale.")
    return merged, warnings


def _merge(group: list[SpanRec], parent_path: tuple[str, ...], children: dict) -> list[AggNode]:
    order: list[str] = []
    buckets: dict[str, list[SpanRec]] = defaultdict(list)
    for s in sorted(group, key=lambda x: x.start_time):
        if s.span_name not in buckets:
            order.append(s.span_name)
        buckets[s.span_name].append(s)
    nodes = []
    for name in order:
        path = parent_path + (name,)
        node = AggNode(name=name, path=path)
        kids: list[SpanRec] = []
        for s in buckets[name]:
            node.count += 1
            node.total_ms += s.duration_ms
            node.min_ms = min(node.min_ms, s.duration_ms)
            node.max_ms = max(node.max_ms, s.duration_ms)
            node.categories.add(s.category)
            for k, v in s.attributes.items():
                node.attributes[k].add(str(v))
            kids.extend(children.get(s.span_id, []))
        node.children = _merge(kids, path, children)
        nodes.append(node)
    return nodes


def _count_child_overflow(nodes: list[AggNode]) -> int:
    n = 0
    for node in nodes:
        if node.children and sum(c.total_ms for c in node.children) > node.total_ms * 1.01 + 0.5:
            n += 1
        n += _count_child_overflow(node.children)
    return n


def _category_label(categories: set) -> str:
    return next(iter(categories)) if len(categories) == 1 else "mixed"


def to_flame_graph(roots: list[AggNode], root_name: str) -> dict:
    """Nested {name, value, children} tree for d3-flame-graph. value = inclusive total duration (ms).

    A synthetic root is always added so runs with several top-level spans still form one tree.
    """
    def node_json(n: AggNode) -> dict:
        return {"name": n.name, "value": round(n.total_ms, 6), "count": n.count, "mean_ms": n.mean_ms,
                "min_ms": n.min_ms, "max_ms": n.max_ms, "category": _category_label(n.categories),
                "attributes": {k: sorted(v) for k, v in n.attributes.items()},
                "children": [node_json(c) for c in n.children]}

    total = sum(r.total_ms for r in roots)
    return {"name": root_name, "value": round(total, 6), "synthetic_root": True, "count": 1, "category": "run",
            "attributes": {}, "children": [node_json(r) for r in roots]}


# ----------------------------------------------------------------------------- deltas


def pct_change(before: float | None, after: float | None) -> float | None:
    if before is None or after is None or before == 0:
        return None
    return (after - before) / abs(before) * 100.0


def compare_spans(baseline: list[AggNode], remediated: list[AggNode]) -> list[dict]:
    rows: list[dict] = []
    _walk_pair(baseline, remediated, 0, rows)
    return rows


def _walk_pair(b_nodes: list[AggNode], r_nodes: list[AggNode], depth: int, rows: list[dict]) -> None:
    b_map = {n.name: n for n in b_nodes}
    r_map = {n.name: n for n in r_nodes}
    names = [n.name for n in b_nodes] + [n.name for n in r_nodes if n.name not in b_map]
    for name in names:
        b, r = b_map.get(name), r_map.get(name)
        path = (b or r).path
        status = MATCHED if b and r else (BASELINE_ONLY if b else REMEDIATED_ONLY)
        notes = []
        row = {"path": list(path), "span_name": name, "depth": depth, "status": status,
               "category_baseline": _category_label(b.categories) if b else None,
               "category_remediated": _category_label(r.categories) if r else None,
               "attributes_baseline": {k: sorted(v) for k, v in b.attributes.items()} if b else None,
               "attributes_remediated": {k: sorted(v) for k, v in r.attributes.items()} if r else None,
               "baseline": b.stats() if b else None, "remediated": r.stats() if r else None,
               "delta_total_ms": None, "pct_total": None, "delta_mean_ms": None, "pct_mean": None, "notes": notes}
        if status == MATCHED:
            row["delta_total_ms"] = r.total_ms - b.total_ms
            row["pct_total"] = pct_change(b.total_ms, r.total_ms)
            row["delta_mean_ms"] = r.mean_ms - b.mean_ms
            row["pct_mean"] = pct_change(b.mean_ms, r.mean_ms)
            if b.total_ms == 0:
                notes.append("baseline total is 0 ms; % change is undefined")
            if b.count != r.count:
                notes.append(f"call count differs (baseline {b.count}, remediated {r.count}); compare mean as well as total")
            if b.categories != r.categories:
                notes.append(f"category differs (baseline {sorted(b.categories)}, remediated {sorted(r.categories)})")
            if row["attributes_baseline"] != row["attributes_remediated"]:
                notes.append("attributes differ between runs (see attributes columns)")
        else:
            notes.append("present in baseline only - no remediated counterpart at this position"
                         if status == BASELINE_ONLY else "present in remediated only - no baseline counterpart at this position")
        rows.append(row)
        _walk_pair(b.children if b else [], r.children if r else [], depth + 1, rows)


def _sample_stats(values: list[float]) -> dict:
    return {"count": len(values), "avg": sum(values) / len(values), "min": min(values), "max": max(values)}


def compare_samples(baseline: list[SampleRec], remediated: list[SampleRec]) -> list[dict]:
    def group(samples):
        g: dict[tuple[str, str], list[SampleRec]] = defaultdict(list)
        for s in sorted(samples, key=lambda x: x.timestamp):
            g[(s.metric_name, s.category)].append(s)
        return g

    bg, rg = group(baseline), group(remediated)
    keys = list(bg) + [k for k in rg if k not in bg]
    rows = []
    for key in keys:
        b, r = bg.get(key), rg.get(key)
        status = MATCHED if b and r else (BASELINE_ONLY if b else REMEDIATED_ONLY)
        b_units = sorted({s.unit for s in b}) if b else []
        r_units = sorted({s.unit for s in r}) if r else []
        row = {"metric_name": key[0], "category": key[1], "status": status,
               "unit": (b_units or r_units)[0] if len(set(b_units + r_units)) == 1 else None,
               "units_baseline": b_units, "units_remediated": r_units,
               "baseline": _sample_stats([s.value for s in b]) if b else None,
               "remediated": _sample_stats([s.value for s in r]) if r else None, "notes": []}
        for stat in ("avg", "min", "max"):
            row[f"delta_{stat}"] = None
            row[f"pct_{stat}"] = None
        if status != MATCHED:
            row["notes"].append("captured in baseline only" if status == BASELINE_ONLY else "captured in remediated only")
        elif row["unit"] is None:
            row["notes"].append(f"units differ (baseline {b_units}, remediated {r_units}); deltas not computed")
        else:
            for stat in ("avg", "min", "max"):
                row[f"delta_{stat}"] = row["remediated"][stat] - row["baseline"][stat]
                row[f"pct_{stat}"] = pct_change(row["baseline"][stat], row["remediated"][stat])
        for side in ("baseline", "remediated"):
            if row[side] and row[side]["count"] < MIN_SAMPLES_PER_METRIC:
                row["notes"].append(f"only {row[side]['count']} {side} sample(s); treat this delta as indicative only")
        rows.append(row)
    return rows


# ----------------------------------------------------------------------------- summary


def _fmt_ms(v: float) -> str:
    return f"{v:,.3f} ms" if abs(v) < 10 else f"{v:,.1f} ms"


def _direction(pct: float | None, up: str, down: str) -> str:
    if pct is None:
        return "change not expressible as %"
    if abs(pct) < MATERIAL_CHANGE_PCT:
        return f"no material change (within ±{MATERIAL_CHANGE_PCT:g}%)"
    return up if pct > 0 else down


def _fmt_pct(p: float | None) -> str:
    return "n/a" if p is None else f"{p:+.1f}%"


def summarize(span_rows: list[dict], sample_rows: list[dict]) -> str:
    """One plain-language paragraph built only from the computed rows."""
    parts: list[str] = []
    top = [r for r in span_rows if r["depth"] == 0 and r["status"] == MATCHED]
    if top:
        b_total = sum(r["baseline"]["total_ms"] for r in top)
        r_total = sum(r["remediated"]["total_ms"] for r in top)
        pct = pct_change(b_total, r_total)
        parts.append(f"Instrumented time in matched top-level spans: remediated {_fmt_ms(r_total)} vs baseline "
                     f"{_fmt_ms(b_total)} ({_fmt_pct(pct)}; {_direction(pct, 'slower', 'faster')}).")

    matched = [r for r in span_rows if r["status"] == MATCHED]
    largest = max(matched, key=lambda r: abs(r["delta_total_ms"]), default=None)
    # Parent durations include their children, so descend to the most specific span that still
    # accounts for >= 90% of that change (e.g. hash_password -> compute_digest).
    while largest:
        prefix = largest["path"]
        kids = [r for r in matched if len(r["path"]) == len(prefix) + 1 and r["path"][:-1] == prefix
                and abs(r["delta_total_ms"]) >= 0.9 * abs(largest["delta_total_ms"])]
        if not kids:
            break
        largest = kids[0]
    if largest:
        parts.append(f"Largest change: '{' > '.join(largest['path'])}' ({largest['category_remediated']}) "
                     f"{_fmt_ms(largest['baseline']['total_ms'])} -> {_fmt_ms(largest['remediated']['total_ms'])} "
                     f"({_fmt_pct(largest['pct_total'])}).")

    by_metric = {(r["metric_name"]): r for r in sample_rows if r["status"] == MATCHED and r["unit"]}
    sampled, few = [], False
    cpu, mem = by_metric.get("cpu_pct"), by_metric.get("memory_mb")
    if cpu:
        cpu_pct_text = ("% change undefined: baseline average is 0" if cpu["pct_avg"] is None and cpu["baseline"]["avg"] == 0
                        else _fmt_pct(cpu["pct_avg"]))
        sampled.append(f"average CPU {cpu['baseline']['avg']:.1f} -> {cpu['remediated']['avg']:.1f} {cpu['unit']} ({cpu_pct_text})")
    if mem:
        sampled.append(f"peak memory {mem['baseline']['max']:.1f} -> {mem['remediated']['max']:.1f} {mem['unit']} ({_fmt_pct(mem['pct_max'])})")
    for r in (cpu, mem):
        if r and min(r["baseline"]["count"], r["remediated"]["count"]) < MIN_SAMPLES_PER_METRIC:
            few = True
    if sampled:
        parts.append("Sampling: " + "; ".join(sampled) + (" - few samples, indicative only." if few else "."))

    if largest and largest["category_remediated"] == "cryptographic" and (largest["pct_total"] or 0) >= MATERIAL_CHANGE_PCT:
        memory_up = bool(mem and mem["pct_max"] is not None and mem["pct_max"] >= MATERIAL_CHANGE_PCT)
        cpu_up = bool(cpu and ((cpu["pct_avg"] is not None and cpu["pct_avg"] >= MATERIAL_CHANGE_PCT) or
                               (cpu["pct_avg"] is None and cpu["baseline"]["avg"] == 0 and cpu["remediated"]["avg"] > 0)))
        if memory_up:
            uses = " and ".join((["more CPU"] if cpu_up else []) + ["more memory"])
            parts.append(f"Remediated cryptographic work is slower and uses {uses} than baseline, "
                         "consistent with a deliberately slow, memory-hard algorithm.")
        else:
            parts.append("Remediated cryptographic work is slower than baseline" + (" and uses more CPU" if cpu_up else "") +
                         "; sampled peak memory did not rise materially, so memory-hardness is not evidenced by this data.")

    unmatched = sum(1 for r in span_rows if r["status"] != MATCHED)
    if unmatched:
        parts.append(f"{unmatched} span path(s) exist in only one run - see the span table.")
    return " ".join(parts) if parts else "No comparable spans or samples were captured."


# ----------------------------------------------------------------------------- report


def build_report(baseline_run: dict, remediated_run: dict, b_spans: list[SpanRec], r_spans: list[SpanRec],
                 b_samples: list[SampleRec], r_samples: list[SampleRec]) -> dict:
    b_roots, b_warn = build_tree(b_spans)
    r_roots, r_warn = build_tree(r_spans)
    span_rows = compare_spans(b_roots, r_roots)
    sample_rows = compare_samples(b_samples, r_samples)

    warnings = [f"baseline: {w}" for w in b_warn] + [f"remediated: {w}" for w in r_warn]
    for run in (baseline_run, remediated_run):
        if run["completed_at"] is None:
            warnings.append(f"{run['phase']} run {run['run_id']} was never marked complete; data may be partial.")
    for key in ("hostname", "platform", "runtime", "cpu_count"):
        b_val, r_val = baseline_run["metadata"].get(key), remediated_run["metadata"].get(key)
        if b_val is not None and r_val is not None and b_val != r_val:
            warnings.append(f"environment differs between runs ({key}: {b_val} vs {r_val}); deltas may reflect the environment, not the code.")
    if not b_spans or not r_spans:
        warnings.append("at least one run has no spans.")
    warnings.append("Single run vs single run: no repeated trials, so deltas include run-to-run noise. "
                    "Spans aggregated at the same path (count > 1) reduce, but do not remove, that noise.")

    def side(run, roots, spans, samples):
        return {**run, "categories": sorted({s.category for s in spans} | {s.category for s in samples}),
                "span_count": len(spans), "sample_count": len(samples),
                "flame_graph": to_flame_graph(roots, f"{run['phase']}: {run['label']}")}

    return {
        "schema_version": WIRE_SCHEMA_VERSION,
        "service": baseline_run["service"],
        "baseline": side(baseline_run, b_roots, b_spans, b_samples),
        "remediated": side(remediated_run, r_roots, r_spans, r_samples),
        "span_comparison": span_rows,
        "sampling_comparison": sample_rows,
        "summary": summarize(span_rows, sample_rows),
        "warnings": warnings,
        "thresholds": {"material_change_pct": MATERIAL_CHANGE_PCT, "min_samples_per_metric": MIN_SAMPLES_PER_METRIC},
    }
