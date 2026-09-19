"""Exports (Phase 4): CSV and a PDF report of a comparison, plus CSV of the run list.

Every export is built from the same response as the screen (`/v2/compare`, `/v2/runs`), through the same
access checks, so the numbers cannot drift from what people see. CSV cells that a spreadsheet would treat as
a formula (starting with = + - @, tab or CR) get a leading apostrophe - a run's service name or label comes
from the app that sent it, so it must never be able to run as a formula in someone's Excel.
"""

from __future__ import annotations

import csv
import io
import math
import re
from datetime import datetime, timezone

from fastapi import APIRouter, Query, Request
from fastapi.responses import Response

from profiler_service.api_v2 import AccessDep, SessionDep, compare_v2, search_runs
from profiler_service.formatting import fmt_bytes, fmt_duration_ns

router = APIRouter(prefix="/v2", tags=["exports"])
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
# our own change values ("+15.8%", "-3.1% (not significant)") are exact text, not formulas; nothing else gets through
OUR_CHANGE = re.compile(r"[+-]\d+(\.\d+)?%( \(not significant\))?")

# the validated chart palette (see web/src/lib/colors.ts): reference grey, then the candidate slots
REF_COLOR, CANDIDATE_COLORS = "#898781", ("#2a78d6", "#eb6834", "#1baf7a")


def safe_cell(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return repr(round(value, 9)) if math.isfinite(value) else ""
    text = str(value)
    if text.startswith(FORMULA_START) and not OUR_CHANGE.fullmatch(text):
        return "'" + text
    return text


def _csv(rows: list[dict], columns: list[str], filename: str) -> Response:
    buf = io.StringIO()
    buf.write("﻿")  # BOM: Excel opens UTF-8 (µ, ×) correctly
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(columns)
    for r in rows:
        w.writerow([safe_cell(r.get(c)) for c in columns])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")


def _name(result: dict) -> str:
    base = (result.get("experiment") or {}).get("experiment_id") or result.get("operation") or "compare"
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(base))[:40]


def _machine(result: dict) -> str:
    env = (result.get("experiment") or {}).get("env") or {}
    if env.get("cpu_model"):
        return env["cpu_model"]
    hosts = {(r.get("metadata") or {}).get("host.name") for r in result.get("runs") or []} - {None}
    return ", ".join(sorted(hosts)) if hosts else ""


SCORECARD = ["variant", "label", "reference", "algorithm", "params", "safe_for_passwords", "trials", "median_ns", "p95_ns",
             "p99_ns", "range_low_ns", "range_high_ns", "ci95_low_ns", "ci95_high_ns", "percentile_method", "cpu_s_per_op",
             "cores_busy", "peak_rss_bytes", "mem_per_op_bytes", "change_vs_reference", "significant", "cpu_change",
             "capacity_rate_per_s", "capacity_cores_needed", "weak_data_flags", "machine"]
TRIALS = ["variant", "label", "trial", "ops", "p50_ns", "p95_ns", "p99_ns", "mean_ns", "iqr_ns", "wall_s", "noisy",
          "exact_percentiles"]


def scorecard_rows(result: dict) -> list[dict]:
    machine = _machine(result)
    rows = []
    for v in result["variants"]:
        t, sec, vs, cap = v["time_per_call"], v.get("security") or {}, v.get("vs_reference") or {}, v.get("capacity") or {}
        rng, ci = t.get("range_ns") or [None, None], t.get("ci95_ns") or [None, None]
        rows.append({
            "variant": v["key"], "label": v["label"], "reference": v["is_reference"], "algorithm": sec.get("algorithm"),
            "params": sec.get("params"), "safe_for_passwords": sec.get("safe_for_passwords"), "trials": v["trials"],
            "median_ns": t["median_ns"], "p95_ns": t["p95_ns"], "p99_ns": t["p99_ns"], "range_low_ns": rng[0],
            "range_high_ns": rng[1], "ci95_low_ns": ci[0], "ci95_high_ns": ci[1], "percentile_method": t.get("percentile_method"),
            "cpu_s_per_op": v["cpu"]["cpu_s_per_op"], "cores_busy": v["cpu"]["cores_busy"],
            "peak_rss_bytes": v["memory"]["peak_rss_bytes"], "mem_per_op_bytes": v["memory"]["mem_per_op_bytes"],
            "change_vs_reference": vs.get("time_change") if not v["is_reference"] else "reference",
            "significant": vs.get("significant"), "cpu_change": vs.get("cpu_change"),
            "capacity_rate_per_s": cap.get("rate_per_s"), "capacity_cores_needed": cap.get("cores_needed"),
            "weak_data_flags": " ".join(f["code"] for f in v["flags"]), "machine": machine})
    return rows


def trial_rows(result: dict) -> list[dict]:
    return [{"variant": v["key"], "label": v["label"], "trial": p["trial_index"] + 1, "ops": p["ops"], "p50_ns": p["p50"],
             "p95_ns": p["p95"], "p99_ns": p["p99"], "mean_ns": p["mean"], "iqr_ns": p["iqr"], "wall_s": p["wall_s"],
             "noisy": p["noisy"], "exact_percentiles": p["exact"]}
            for v in result["variants"] for p in v["per_trial"]]


@router.get("/compare.csv")
def compare_csv(session: SessionDep, access: AccessDep, experiment_id: str | None = None, run_ids: str | None = None,
                reference: str | None = None, rate: float = Query(100.0, gt=0, le=1e7), cores: int | None = Query(None, ge=1, le=4096),
                kind: str = Query("scorecard", pattern="^(scorecard|trials)$")) -> Response:
    result = compare_v2(session, access, experiment_id, run_ids, reference, rate, cores)
    rows, cols = (scorecard_rows(result), SCORECARD) if kind == "scorecard" else (trial_rows(result), TRIALS)
    return _csv(rows, cols, f"vayunx-{_name(result)}-{kind}-{_stamp()}.csv")


@router.get("/runs.csv")
def runs_csv(session: SessionDep, access: AccessDep, q: str | None = None, service: str | None = None,
             algorithm: str | None = None, source: str | None = None, project: str | None = None) -> Response:
    rows, offset = [], 0
    while True:  # every matching run, a page at a time
        page = search_runs(session, access, q=q, service=service, algorithm=algorithm, source=source, date_from=None,
                           date_to=None, project=project, limit=500, offset=offset)
        rows += page["items"]
        offset += 500
        if offset >= page["total"]:
            break
    cols = ["run_id", "project_id", "source", "service", "variant", "label", "phase", "created_at", "completed_at",
            "experiment_id", "trial_index", "span_count", "sample_count", "op_stat_count"]
    return _csv(rows, cols, f"vayunx-runs-{_stamp()}.csv")


# ----------------------------------------------------------------------------- PDF


def _tick(ns: float) -> str:
    """Axis labels at powers of ten, without decimals ("10 ms", not "10.0 ms"), as in the UI."""
    for unit, size in (("s", 1e9), ("ms", 1e6), ("µs", 1e3), ("ns", 1)):
        if ns >= size:
            return f"{ns / size:g} {unit}"
    return f"{ns:g} ns"


def _dot_plot(result: dict, width: float):
    """Median (dot) with the p95 whisker per variant on a log time axis. Grey = reference; a table has the numbers."""
    from reportlab.graphics.shapes import Circle, Drawing, Line, String
    from reportlab.lib.colors import HexColor

    variants = [v for v in result["variants"] if v["time_per_call"]["median_ns"]]
    row_h, left, right, top = 22, 170, 20, 10
    height = top + row_h * len(variants) + 34
    d = Drawing(width, height)
    values = [x for v in variants for x in (v["time_per_call"]["median_ns"], v["time_per_call"]["p95_ns"]) if x]
    lo = 10 ** math.floor(math.log10(min(values)))
    hi = 10 ** math.ceil(math.log10(max(values)))
    if hi <= lo:
        hi = lo * 10
    x = lambda ns: left + (math.log10(ns) - math.log10(lo)) / (math.log10(hi) - math.log10(lo)) * (width - left - right)  # noqa: E731
    base_y = 26
    axis = HexColor("#c3c2b7")
    d.add(Line(left, base_y, width - right, base_y, strokeColor=axis, strokeWidth=0.8))
    decade = lo
    while decade <= hi * 1.0001:
        d.add(Line(x(decade), base_y, x(decade), height - top, strokeColor=HexColor("#e1e0d9"), strokeWidth=0.5))
        d.add(String(x(decade), base_y - 11, _tick(decade), fontName="Helvetica", fontSize=7, fillColor=HexColor("#6f6d68"), textAnchor="middle"))
        decade *= 10
    d.add(String(left + (width - left - right) / 2, 2, "Time per call (log scale): dot = median, line = up to p95",
                 fontName="Helvetica", fontSize=7, fillColor=HexColor("#6f6d68"), textAnchor="middle"))
    slot = 0
    for i, v in enumerate(variants):
        if v["is_reference"]:
            color = HexColor(REF_COLOR)
        else:
            color = HexColor(CANDIDATE_COLORS[slot % len(CANDIDATE_COLORS)])
            slot += 1
        y = height - top - row_h * i - row_h / 2
        t = v["time_per_call"]
        label = v["label"] if len(v["label"]) <= 34 else v["label"][:16] + "..." + v["label"][-15:]
        d.add(String(left - 8, y - 3, label + (" (reference)" if v["is_reference"] else ""), fontName="Helvetica", fontSize=7.5,
                     fillColor=HexColor("#0b0b0b"), textAnchor="end"))
        if t["p95_ns"]:
            d.add(Line(x(t["median_ns"]), y, x(t["p95_ns"]), y, strokeColor=color, strokeWidth=2))
        d.add(Circle(x(t["median_ns"]), y, 4, fillColor=color, strokeColor=HexColor("#ffffff"), strokeWidth=1))
    return d


def compare_pdf_bytes(result: dict) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    from xml.sax.saxutils import escape

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=16 * mm, rightMargin=16 * mm, topMargin=16 * mm, bottomMargin=16 * mm,
                            title="VAYUNX crypto comparison", author="VAYUNX Crypto Profiler", pageCompression=0)
    st = getSampleStyleSheet()
    small = st["BodyText"].clone("small", fontSize=8, leading=10, textColor=colors.HexColor("#52514e"))
    body = st["BodyText"].clone("body", fontSize=9.5, leading=12.5)
    exp = result.get("experiment") or {}
    env = exp.get("env") or {}
    what = exp.get("label") or (f"My app: {result.get('operation')}" if result.get("operation") else "Comparison")
    story = [Paragraph("VAYUNX crypto comparison", st["Title"]),
             Paragraph(escape(what), st["Heading3"]),
             Paragraph(escape(f"Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · source: "
                              f"{result['source']} · reference: {result['reference']}"), small)]
    machine = _machine(result)
    if env:
        machine = f"{env.get('cpu_model')} · {env.get('cpu_count_logical')} logical CPUs · {env.get('os')} · {env.get('runtime')}"
    if machine:
        story.append(Paragraph(escape(f"Measured on: {machine}"), small))
    story += [Spacer(1, 6), Paragraph(f"<b>{escape(result['verdict'])}</b>", body), Spacer(1, 8)]

    header = ["Algorithm", "Time per call", "p95", "Change", "CPU / call", "Peak RAM", "Passwords"]
    rows = [header]
    for v in result["variants"]:
        t, vs, sec = v["time_per_call"], v.get("vs_reference") or {}, v.get("security") or {}
        cpu = v["cpu"]["cpu_s_per_op"]
        rows.append([Paragraph(escape(v["label"] + (" (reference)" if v["is_reference"] else "")), small),
                     fmt_duration_ns(t["median_ns"]), fmt_duration_ns(t["p95_ns"]),
                     "reference" if v["is_reference"] else (vs.get("time_change") or "–"),
                     fmt_duration_ns(cpu * 1e9) if cpu else "not measured", fmt_bytes(v["memory"]["peak_rss_bytes"]),
                     {True: "Safe", False: "Not for passwords"}.get(sec.get("safe_for_passwords"), "–")])
    table = Table(rows, colWidths=[52 * mm, 22 * mm, 20 * mm, 26 * mm, 22 * mm, 18 * mm, 20 * mm], repeatRows=1)
    table.setStyle(TableStyle([
        ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 8), ("FONT", (0, 1), (-1, -1), "Helvetica", 8),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#52514e")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.HexColor("#c3c2b7")),
        ("LINEBELOW", (0, 1), (-1, -1), 0.4, colors.HexColor("#e1e0d9")), ("ALIGN", (1, 0), (5, -1), "RIGHT"),
    ]))
    story += [table, Spacer(1, 10), _dot_plot(result, doc.width), Spacer(1, 8)]

    flags = [(v["label"], f) for v in result["variants"] for f in v["flags"]]
    if flags:
        story.append(Paragraph("Weak data", st["Heading4"]))
        for label, f in flags:
            story.append(Paragraph(escape(f"{label}: {f['message']} (affects {', '.join(f.get('affects') or [])})"), small))
    story.append(Paragraph("Security", st["Heading4"]))
    for v in result["variants"]:
        sec = v.get("security")
        if sec:
            story.append(Paragraph(escape(f"{v['label']}: {sec['summary']}"), small))
    ref = next((v for v in result["variants"] if v["is_reference"]), None)
    story.append(Paragraph(f"Capacity estimate at {result['capacity_defaults']['rate_per_s']:g} calls per second", st["Heading4"]))
    for v in result["variants"]:
        c = v["capacity"]
        need = f"{c['cores_needed']:.1f} of {c['cores_total']} cores" if c.get("cores_needed") is not None else "CPU per call not measured"
        extra = "" if v is ref or c.get("added_latency_ns") is None else f" · +{fmt_duration_ns(c['added_latency_ns'])} per call"
        story.append(Paragraph(escape(f"{v['label']}: {need}{extra}"), small))
    story.append(Paragraph("Method", st["Heading4"]))
    for k, text in (result.get("method") or {}).items():
        story.append(Paragraph(escape(f"{k.replace('_', ' ')}: {text}"), small))
    for note in result.get("measurement_notes") or []:
        story.append(Paragraph(escape(note), small))
    story += [Spacer(1, 6), Paragraph("Every number above was measured on the machine named at the top; estimates are "
                                      "labelled. Other machines will differ.", small)]
    doc.build(story)
    return buf.getvalue()


@router.get("/compare.pdf")
def compare_pdf(session: SessionDep, access: AccessDep, experiment_id: str | None = None, run_ids: str | None = None,
                reference: str | None = None, rate: float = Query(100.0, gt=0, le=1e7),
                cores: int | None = Query(None, ge=1, le=4096)) -> Response:
    result = compare_v2(session, access, experiment_id, run_ids, reference, rate, cores)
    return Response(compare_pdf_bytes(result), media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="vayunx-{_name(result)}-{_stamp()}.pdf"'})
