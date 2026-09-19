"""Crypto inventory (Phase 4): which algorithms each app was seen using - from its measured data only.

One entry per (app, operation, algorithm, parameters), built from the op-stats of app runs (Lab runs are
benchmarks, not apps) in the projects the caller can see. Each entry carries the evidence (calls, runs,
variants, first/last seen, the code paths it ran in) and a security note: whether the algorithm is fit for
password storage and, for password hashes whose parameters were recorded, whether they meet the OWASP
minimums. `key` ("service|operation|algorithm|params") is stable, for linking to the VAYUNX crypto inventory.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import select

from profiler_service.access import access_for, scope_query
from profiler_service.db import iso_utc
from profiler_service.exports import _csv
from profiler_service.models import OpStat, Run
from vayunx_lab.security import note_for_algorithm, owasp_check

router = APIRouter(prefix="/v2", tags=["inventory"])


def _runtime(meta: dict) -> str:
    name, version = meta.get("process.runtime.name"), meta.get("process.runtime.version")
    if name:
        return f"{name} {version}".strip() if version else str(name)
    return str(meta.get("runtime") or meta.get("telemetry.sdk.language") or "")


def build_inventory(request: Request, project: str | None = None, service: str | None = None) -> dict:
    with request.app.state.sessionmaker() as s:
        access = access_for(request, s)
        runs_q = scope_query(select(Run), access, Run, project, s).where(Run.source == "app")
        if service:
            runs_q = runs_q.where(Run.service == service)
        runs = {r.run_id: r for r in s.scalars(runs_q)}
        entries: dict[tuple, dict] = {}
        if runs:
            rows = s.execute(select(OpStat.run_id, OpStat.op_name, OpStat.attributes_json, OpStat.count,
                                    OpStat.interval_start, OpStat.interval_end)
                             .where(OpStat.run_id.in_(list(runs)))).all()
        else:
            rows = []
        for run_id, op_name, attrs_json, count, start, end in rows:
            run = runs[run_id]
            a = json.loads(attrs_json)
            algorithm = a.get("crypto.algorithm")
            if not algorithm:
                continue  # not crypto data (e.g. a v1 SDK op without crypto attributes)
            operation = a.get("crypto.operation") or op_name
            params = a.get("crypto.params") or None
            k = (run.service, operation, algorithm, params)
            e = entries.get(k)
            if e is None:
                e = entries[k] = {"service": run.service, "operation": operation, "algorithm": algorithm, "params": params,
                                  "calls": 0, "runs": set(), "variants": set(), "scopes": set(), "first": start,
                                  "last": end, "library": None, "runtime": "", "_lib_at": None, "projects": set()}
            e["calls"] += int(count)
            e["runs"].add(run_id)
            e["projects"].add(run.project_id)
            if run.variant:
                e["variants"].add(run.variant)
            if a.get("vayunx.scope"):
                e["scopes"].add(a["vayunx.scope"])
            e["first"], e["last"] = min(e["first"], start), max(e["last"], end)
            if e["_lib_at"] is None or end >= e["_lib_at"]:  # the most recent library / runtime seen
                e["library"], e["_lib_at"] = a.get("crypto.library"), end
                e["runtime"] = _runtime(json.loads(run.metadata_json or "{}"))

    apps: dict[str, list[dict]] = defaultdict(list)
    for (svc, operation, algorithm, params), e in sorted(entries.items(), key=lambda kv: (kv[0][0], -kv[1]["calls"])):
        note = note_for_algorithm(algorithm, params)
        meets, why = owasp_check(algorithm, params)
        apps[svc].append({
            "key": f"{svc}|{operation}|{algorithm}|{params or ''}", "operation": operation, "algorithm": algorithm,
            "params": params, "library": e["library"], "runtime": e["runtime"], "calls": e["calls"], "runs": len(e["runs"]),
            "variants": sorted(e["variants"]), "scopes": sorted(e["scopes"]), "projects": sorted(e["projects"]),
            "first_seen": iso_utc(e["first"]), "last_seen": iso_utc(e["last"]),
            "safe_for_passwords": note["safe_for_passwords"] if note else None,
            "meets_owasp_minimum": meets, "owasp_note": why, "security_summary": note["summary"] if note else None,
        })
    out = []
    for svc, items in sorted(apps.items()):
        out.append({"service": svc, "algorithms": items, "summary": {
            "algorithms": len(items),
            "not_for_passwords": sum(1 for i in items if i["safe_for_passwords"] is False),
            "below_owasp_minimum": sum(1 for i in items if i["safe_for_passwords"] and i["meets_owasp_minimum"] is False)}})
    return {"generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"), "apps": out,
            "note": "Built only from what the SDKs measured in app runs. An algorithm an app has but did not run is not listed."}


@router.get("/inventory")
def inventory(request: Request, project: str | None = None, service: str | None = None) -> dict:
    return build_inventory(request, project, service)


@router.get("/inventory.csv")
def inventory_csv(request: Request, project: str | None = None, service: str | None = None) -> Response:
    inv = build_inventory(request, project, service)
    rows = [{**i, "service": a["service"], "scopes": " ".join(i["scopes"]), "variants": " ".join(i["variants"]),
             "projects": " ".join(i["projects"])} for a in inv["apps"] for i in a["algorithms"]]
    cols = ["service", "key", "operation", "algorithm", "params", "library", "runtime", "scopes", "calls", "runs", "variants",
            "projects", "first_seen", "last_seen", "safe_for_passwords", "meets_owasp_minimum", "owasp_note"]
    return _csv(rows, cols, f"vayunx-crypto-inventory-{datetime.now(timezone.utc).strftime('%Y%m%d')}.csv")
