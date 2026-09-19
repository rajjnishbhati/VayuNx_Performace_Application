"""CI gate (Phase 4): judge one run's measured crypto latency against rules, e.g. "login p95 < 250ms".

A rule is `<name> <stat> <op> <value>`:
* name: a code path the app named with vayunx.span/measure (e.g. `login`), or a crypto operation (`hash`,
  `verify`, `kdf`...) - matched exactly like "My app" (see api_v2._op_key);
* stat: p50 | p95 | p99 | mean | max (a time: ns, us/µs, ms, s) or calls (a count);
* op: < <= > >=.

Result per rule: pass, fail, or no_data (the name was not recorded, or with fewer than `min_calls` calls -
too few to judge is never reported as a pass). Percentiles come from the log2x8 histogram (±6.25 %); a
measurement within that precision of the limit is marked `near_limit`, so a pass by a hair is visible.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from sqlalchemy import select

from profiler_service.access import access_for, load_run, scope_query
from profiler_service.api_v2 import _label, _op_key, problem
from profiler_service.db import iso_utc
from profiler_service.formatting import fmt_duration_ns
from profiler_service.models import OpStat, Run
from vayunx_profiler_sdk.histogram import LatencyHistogram

router = APIRouter(prefix="/v2", tags=["gate"])
UNITS = {"ns": 1, "us": 1e3, "µs": 1e3, "ms": 1e6, "s": 1e9}
TIME_STATS = ("p50", "p95", "p99", "mean", "max")
OPS = {"<": lambda a, b: a < b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}
PRECISION = 0.0625  # half a log2x8 bucket
RULE = re.compile(r"^\s*(?P<name>[^\s]+)\s+(?P<stat>[a-z0-9]+)\s*(?P<op><=|>=|<|>)\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>ns|us|µs|ms|s)?\s*$")


class GateRuleError(ValueError):
    pass


@dataclass
class Rule:
    text: str
    name: str
    stat: str
    op: str
    limit: float  # ns for time stats, a count for calls


def parse_rule(text: str) -> Rule:
    m = RULE.match(text or "")
    if not m:
        raise GateRuleError(f"cannot read the rule {text!r}; write it like \"login p95 < 250ms\" or \"login calls >= 100\"")
    stat, unit, num = m["stat"], m["unit"], float(m["num"])
    if stat == "calls":
        if unit:
            raise GateRuleError(f"{text!r}: calls is a count, it takes no unit")
        return Rule(text.strip(), m["name"], stat, m["op"], num)
    if stat not in TIME_STATS:
        raise GateRuleError(f"{text!r}: the statistic must be one of {', '.join(TIME_STATS)} or calls")
    if not unit:
        raise GateRuleError(f"{text!r}: give the limit a unit (ns, us, ms or s)")
    return Rule(text.strip(), m["name"], stat, m["op"], num * UNITS[unit])


class GateIn(BaseModel):
    rules: list[str] = Field(min_length=1, max_length=50)
    run_id: str | None = None
    service: str | None = None
    variant: str | None = None
    project: str | None = None
    min_calls: int = Field(default=30, ge=1, le=10_000_000)


def _histograms(session, run_id: str) -> dict[str, LatencyHistogram]:
    out: dict[str, LatencyHistogram] = {}
    for row in session.scalars(select(OpStat).where(OpStat.run_id == run_id)):
        attrs = json.loads(row.attributes_json)
        h = LatencyHistogram.from_dict(json.loads(row.histogram_json))
        for key in {_label(_op_key(attrs, row.op_name)), attrs.get("crypto.operation") or row.op_name}:
            out.setdefault(key, LatencyHistogram()).merge(h)  # a span name and its operations are both addressable
    return out


def _measure(h: LatencyHistogram, stat: str) -> float:
    if stat == "calls":
        return float(h.count)
    if stat == "mean":
        return h.sum_ns / h.count
    if stat == "max":
        return float(h.max_ns)
    return float(h.percentile(float(stat[1:])))


def evaluate(hists: dict[str, LatencyHistogram], rules: list[Rule], min_calls: int) -> dict:
    checks = []
    for r in rules:
        h = hists.get(r.name)
        base = {"rule": r.text, "name": r.name, "stat": r.stat, "op": r.op, "limit": r.limit}
        if h is None or not h.count:
            known = ", ".join(sorted(hists)) or "nothing"
            checks.append({**base, "status": "no_data", "measured": None,
                           "message": f"{r.name!r} was not recorded in this run (recorded: {known})."})
            continue
        if r.stat != "calls" and h.count < min_calls:
            checks.append({**base, "status": "no_data", "measured": None, "calls": h.count,
                           "message": f"only {h.count} calls of {r.name!r}; at least {min_calls} are needed to judge {r.stat}."})
            continue
        v = _measure(h, r.stat)
        ok = OPS[r.op](v, r.limit)
        text = f"{int(v)} calls" if r.stat == "calls" else fmt_duration_ns(v)
        near = r.stat in ("p50", "p95", "p99") and r.limit > 0 and abs(v - r.limit) / r.limit <= PRECISION
        checks.append({**base, "status": "pass" if ok else "fail", "measured": v, "measured_text": text, "calls": h.count,
                       "near_limit": near,
                       "message": f"{r.name} {r.stat} = {text} ({'meets' if ok else 'breaks'} {r.op} "
                                  f"{int(r.limit) if r.stat == 'calls' else fmt_duration_ns(r.limit)})"
                                  + ("; within the ±6.25% histogram precision of the limit" if near else "")})
    statuses = {c["status"] for c in checks}
    overall = "fail" if "fail" in statuses else "no_data" if "no_data" in statuses else "pass"
    return {"status": overall, "checks": checks}


@router.post("/gate")
def gate(request: Request, body: GateIn) -> dict:
    try:
        rules = [parse_rule(r) for r in body.rules]
    except GateRuleError as exc:
        raise problem(422, str(exc), 'Rules look like "login p95 < 250ms" or "login calls >= 100".') from None
    with request.app.state.sessionmaker() as s:
        access = access_for(request, s)
        if body.run_id:
            run = load_run(s, access, body.run_id)
        elif body.service:
            q = scope_query(select(Run), access, Run, body.project, s).where(Run.service == body.service)
            if body.variant:
                q = q.where(Run.variant == body.variant)
            run = s.scalars(q.order_by(Run.created_at.desc()).limit(1)).first()
            if run is None:
                raise problem(404, f"No run of service {body.service!r}"
                                   f"{f' with variant {body.variant!r}' if body.variant else ''}.",
                              "Check the service name and variant your app reports (VAYUNX_SERVICE / VAYUNX_VARIANT).")
        else:
            raise problem(422, "Say which run to judge.", "Pass run_id, or service (and optionally variant) for its latest run.")
        result = evaluate(_histograms(s, run.run_id), rules, body.min_calls)
        result["run"] = {"run_id": run.run_id, "service": run.service, "variant": run.variant, "project_id": run.project_id,
                         "created_at": iso_utc(run.created_at)}
        return result
