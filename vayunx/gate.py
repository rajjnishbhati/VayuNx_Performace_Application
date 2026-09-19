"""CI gate: fail the build when a measured crypto latency breaks a limit.

    python -m vayunx gate --run-id <id> --rule "login p95 < 250ms" [--rule "login calls >= 100"]
    python -m vayunx gate --service my-api --variant argon2id --rule "login p95 < 250ms" --junit gate.xml

Asks the Profiler Service (VAYUNX_ENDPOINT, default http://127.0.0.1:8010; VAYUNX_API_TOKEN when it requires
sign-in) to judge the run. Exit codes: 0 all rules pass · 1 a rule fails · 3 no data to judge (a name not
recorded, or too few calls) · 2 usage, rule or connection error. Writes JUnit XML with --junit, and a
Markdown table to $GITHUB_STEP_SUMMARY when that is set (GitHub Actions).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from xml.sax.saxutils import escape, quoteattr

EXIT = {"pass": 0, "fail": 1, "no_data": 3}
LABEL = {"pass": "PASS", "fail": "FAIL", "no_data": "NO DATA"}


def _post(endpoint: str, body: dict, token: str | None) -> tuple[int, dict]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{endpoint.rstrip('/')}/v2/gate", data=json.dumps(body).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except ValueError:
            return e.code, {}


def junit(result: dict) -> str:
    checks = result["checks"]
    fails = sum(c["status"] == "fail" for c in checks)
    skipped = sum(c["status"] == "no_data" for c in checks)
    run = result.get("run", {})
    lines = [f'<?xml version="1.0" encoding="UTF-8"?>',
             f'<testsuite name="vayunx-gate" tests="{len(checks)}" failures="{fails}" skipped="{skipped}" '
             f'timestamp={quoteattr(run.get("created_at") or "")}>',
             f'  <properties><property name="run_id" value={quoteattr(run.get("run_id") or "")}/>'
             f'<property name="service" value={quoteattr(run.get("service") or "")}/></properties>']
    for c in checks:
        lines.append(f'  <testcase classname="vayunx.gate" name={quoteattr(c["rule"])}>')
        if c["status"] == "fail":
            lines.append(f'    <failure message={quoteattr(c["message"])}>{escape(c["message"])}</failure>')
        elif c["status"] == "no_data":
            lines.append(f'    <skipped message={quoteattr(c["message"])}/>')
        else:
            lines.append(f'    <system-out>{escape(c["message"])}</system-out>')
        lines.append("  </testcase>")
    lines.append("</testsuite>")
    return "\n".join(lines) + "\n"


def summary_markdown(result: dict) -> str:
    run = result.get("run", {})
    icon = {"pass": "✅", "fail": "❌", "no_data": "⚠️"}
    out = [f"### VAYUNX crypto gate: {LABEL[result['status']]}", "",
           f"Run `{run.get('run_id')}` · service `{run.get('service')}` · variant `{run.get('variant')}`", "",
           "| Rule | Result | Measured |", "|---|---|---|"]
    for c in result["checks"]:
        out.append(f"| `{c['rule']}` | {icon[c['status']]} {LABEL[c['status']]} | {c.get('measured_text') or c['message']} |")
    return "\n".join(out) + "\n\n"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="vayunx gate", description=__doc__.splitlines()[0])
    p.add_argument("--endpoint", default=os.environ.get("VAYUNX_ENDPOINT", "http://127.0.0.1:8010"))
    p.add_argument("--run-id", help="the run to judge")
    p.add_argument("--service", help="judge the latest run of this service (VAYUNX_SERVICE of the app)")
    p.add_argument("--variant", help="...with this variant")
    p.add_argument("--project", help="...in this project")
    p.add_argument("--rule", action="append", required=True, help='e.g. "login p95 < 250ms"; repeat for more')
    p.add_argument("--min-calls", type=int, default=30, help="fewer calls than this is 'no data', not a pass (default 30)")
    p.add_argument("--junit", help="write JUnit XML here")
    a = p.parse_args(argv)
    if not a.run_id and not a.service:
        p.error("give --run-id, or --service (and optionally --variant)")
    body = {"rules": a.rule, "min_calls": a.min_calls, "run_id": a.run_id, "service": a.service, "variant": a.variant,
            "project": a.project}
    try:
        status, result = _post(a.endpoint, {k: v for k, v in body.items() if v is not None}, os.environ.get("VAYUNX_API_TOKEN"))
    except (urllib.error.URLError, OSError) as exc:
        print(f"vayunx gate: cannot reach the Profiler Service at {a.endpoint} ({exc})", file=sys.stderr)
        return 2
    if status != 200:
        d = result.get("detail") if isinstance(result, dict) else None
        msg = f"{d.get('error')} {d.get('fix', '')}" if isinstance(d, dict) else json.dumps(d or result)
        print(f"vayunx gate: {msg.strip()} (HTTP {status})", file=sys.stderr)
        return 2
    run = result["run"]
    print(f"VAYUNX crypto gate - run {run['run_id']} ({run['service']}{' / ' + run['variant'] if run.get('variant') else ''})")
    for c in result["checks"]:
        print(f"  {LABEL[c['status']]:<8} {c['rule']}: {c['message']}")
    print(f"Result: {LABEL[result['status']]}")
    if a.junit:
        with open(a.junit, "w", encoding="utf-8") as f:
            f.write(junit(result))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(summary_markdown(result))
    return EXIT[result["status"]]


if __name__ == "__main__":
    sys.exit(main())
