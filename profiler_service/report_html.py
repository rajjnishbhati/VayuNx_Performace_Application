"""HTML pages: run picker and baseline-vs-remediated report.

Flame graphs are rendered by d3-flame-graph 4.1.3 (+ d3 7.9.0) loaded from jsdelivr - no
hand-built flame graph layout here. If the CDN is unreachable the page says so and falls back
to an indented text tree of the same data.
"""

from __future__ import annotations

import json
from html import escape

from profiler_service.formatting import fmt_cores_value, fmt_duration_ms, fmt_mib

D3 = "https://cdn.jsdelivr.net/npm/d3@7.9.0/dist/d3.min.js"
FLAME_JS = "https://cdn.jsdelivr.net/npm/d3-flame-graph@4.1.3/dist/d3-flamegraph.min.js"
FLAME_CSS = "https://cdn.jsdelivr.net/npm/d3-flame-graph@4.1.3/dist/d3-flamegraph.css"

CSS = """
:root{--bg:#f6f6f3;--fg:#1c1c1e;--muted:#66666c;--card:#fff;--line:#e2e2de;--base:#1f5fbf;--rem:#b3261e;--warn:#8a5a00;--good:#1f7a3a}
*{box-sizing:border-box} body{background:var(--bg);color:var(--fg);font:14px/1.5 system-ui,Segoe UI,sans-serif;margin:0;padding:24px 16px}
main{max-width:1200px;margin:0 auto} h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:28px 0 8px}
.muted{color:var(--muted)} .card{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin:12px 0}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:12px}
.scroll{overflow-x:auto} table{border-collapse:collapse;width:100%} th,td{text-align:left;padding:5px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{font-size:12px;color:var(--muted);font-weight:600} td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
code{font-size:12px} .badge{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:700;color:#fff}
.baseline{background:var(--base)} .remediated{background:var(--rem)} .phase-card.b{border-top:4px solid var(--base)} .phase-card.r{border-top:4px solid var(--rem)}
.summary{font-size:15px;border-left:4px solid var(--fg)} .warn{border-left:4px solid var(--warn);background:#fffaf0}
.status{font-size:12px;font-weight:600} .status.baseline_only,.status.remediated_only{color:var(--warn)}
.flame-card h3{margin:0 0 6px;font-size:15px} .details{min-height:1.5em;font-size:12px;color:var(--muted);font-family:ui-monospace,Consolas,monospace}
.fallback{white-space:pre;font-family:ui-monospace,Consolas,monospace;font-size:12px}
label.toggle{margin-right:14px} select{max-width:100%}
"""


def _page(title: str, body: str, head_extra: str = "") -> str:
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{escape(title)}</title><style>{CSS}</style>{head_extra}</head><body><main>{body}</main></body></html>")


def _num(v, digits=3) -> str:
    return "-" if v is None else f"{v:,.{digits}f}"


def _pct(v) -> str:
    return "-" if v is None else f"{v:+.1f}%"


def render_error(status: int, cause: str, fix: str) -> str:
    title = {404: "Not found", 422: "These runs can't be compared"}.get(status, "Something went wrong")
    body = (f"<h1>{escape(title)}</h1><div class='card warn'><p><b>What happened:</b> {escape(cause)}</p>"
            f"<p><b>How to fix it:</b> {escape(fix)}</p></div>"
            "<p><a href=\"/\">← Back to all runs</a></p>")
    return _page(title, body)


def _pager(page: int, page_size: int, total: int) -> str:
    pages = max(1, -(-total // page_size))
    prev_link = f"<a href='/?page={page - 1}&amp;page_size={page_size}'>← Previous</a>" if page > 1 else ""
    next_link = f"<a href='/?page={page + 1}&amp;page_size={page_size}'>Next →</a>" if page < pages else ""
    return f"<p class='muted'>{prev_link} Page {page} of {pages} ({total} runs) {next_link}</p>"


def render_index(runs: list[dict], page: int = 1, page_size: int = 50, total: int | None = None) -> str:
    total = len(runs) if total is None else total
    services = sorted({r["service"] for r in runs})
    rows = "".join(
        f"<tr><td>{escape(r['service'])}</td><td><span class='badge {escape(r['phase'])}'>{escape(r['phase'])}</span></td>"
        f"<td>{escape(r['label'])}</td><td><code>{escape(r['run_id'])}</code></td><td class='num'>{r['span_count']}</td>"
        f"<td class='num'>{r['sample_count']}</td><td class='muted'>{escape(r['created_at'])}</td>"
        f"<td>{'complete' if r['completed_at'] else '<b>not completed</b>'}</td></tr>" for r in runs)

    def options(phase):
        return "".join(f"<option value='{escape(r['run_id'])}'>{escape(r['service'])} · {escape(r['label'])} · {escape(r['run_id'][:12])}</option>"
                       for r in runs if r["phase"] == phase)

    body = (
        "<h1>VAYUNX Profiler Service</h1><p class='muted'>Standalone proof of concept - spans (instrumentation) and periodic system-metric "
        "samples. Demo build, no auth.</p>"
        "<h2>Compare two runs</h2><div class='card'><form action='/report' method='get' class='grid2'>"
        f"<label>Baseline run<br><select name='baseline_run_id' required>{options('baseline')}</select></label>"
        f"<label>Remediated run<br><select name='remediated_run_id' required>{options('remediated')}</select></label>"
        "<div><button type='submit'>Open report</button> <span class='muted'>Both runs must belong to the same service.</span></div></form></div>"
        f"<h2>Runs · services on this page: {escape(', '.join(services)) or 'none'}</h2>"
        f"{_pager(page, page_size, total)}"
        "<div class='card scroll'><table><tr><th>Service</th><th>Phase</th><th>Label</th><th>Run ID</th><th>Spans</th><th>Samples</th><th>Created (UTC)</th><th>State</th></tr>"
        f"{rows or '<tr><td colspan=8 class=muted>No runs yet - run demos/run_demo.py</td></tr>'}</table></div>"
        "<p class='muted'>API docs: <a href='/docs'>/docs</a> · wire format: README.md</p>")
    return _page("VAYUNX Profiler Service", body)


def _run_card(run: dict, cls: str) -> str:
    meta = run.get("metadata") or {}
    env = ", ".join(f"{k}={meta[k]}" for k in ("sdk", "runtime", "platform", "hostname", "pid") if k in meta)
    return (f"<div class='card phase-card {cls}'><span class='badge {escape(run['phase'])}'>{escape(run['phase'].upper())}</span> "
            f"<b>{escape(run['label'])}</b><br><span class='muted'>run_id</span> <code>{escape(run['run_id'])}</code><br>"
            f"<span class='muted'>service</span> {escape(run['service'])} · <span class='muted'>categories</span> {escape(', '.join(run['categories']) or '-')}<br>"
            f"<span class='muted'>created</span> {escape(run['created_at'])} · <span class='muted'>completed</span> {escape(run['completed_at'] or 'NOT COMPLETED')}<br>"
            f"<span class='muted'>spans</span> {run['span_count']} · <span class='muted'>samples</span> {run['sample_count']}"
            f"{f'<br><span class=muted>env</span> <code>{escape(env)}</code>' if env else ''}</div>")


def _attrs(a) -> str:
    return "" if not a else escape("; ".join(f"{k}={'/'.join(v)}" for k, v in a.items()))


def render_report(p: dict) -> str:
    b, r = p["baseline"], p["remediated"]
    def side(row, key, field):
        return row[key][field] if row[key] else None

    span_rows = "".join(
        f"<tr><td style='padding-left:{8 + 18 * row['depth']}px'><code>{escape(row['span_name'])}</code></td>"
        f"<td><span class='status {row['status']}'>{escape(row['status'].replace('_', ' '))}</span></td>"
        f"<td class='num'>{fmt_duration_ms(side(row, 'baseline', 'mean_ms'))}</td>"
        f"<td class='num'>{fmt_duration_ms(side(row, 'remediated', 'mean_ms'))}</td>"
        f"<td class='num'><b>{escape(row['change_per_call'] or '-')}</b></td>"
        f"<td class='num'>{side(row, 'baseline', 'count') or '-'} / {side(row, 'remediated', 'count') or '-'}</td>"
        f"<td class='num'>{fmt_duration_ms(side(row, 'baseline', 'total_ms'))} → {fmt_duration_ms(side(row, 'remediated', 'total_ms'))}</td>"
        f"<td class='muted'>{escape(row['category_remediated'] or row['category_baseline'] or '-')}"
        f"{'<br>' + _attrs(row['attributes_baseline']) if row['attributes_baseline'] else ''}"
        f"{' → ' + _attrs(row['attributes_remediated']) if row['attributes_remediated'] != row['attributes_baseline'] else ''}"
        f"{''.join('<br>' + escape(n) for n in row['notes'])}</td></tr>" for row in p["span_comparison"])

    def metric_value(row, key, stat):
        v = side(row, key, stat)
        return fmt_cores_value(v) if row["metric_name"] == "cpu_pct" else (fmt_mib(v) if row["unit"] == "MiB" else _num(v, 2))

    metric_label = {"cpu_pct": "CPU (cores busy)", "memory_mb": "Memory (RSS)", "num_threads": "Threads"}
    sample_rows = "".join(
        f"<tr><td>{escape(metric_label.get(row['metric_name'], row['metric_name']))}"
        f"<br><span class='muted'>{escape(row['category'])}</span></td>"
        f"<td class='num'>{metric_value(row, 'baseline', 'avg')} / {metric_value(row, 'baseline', 'max')}</td>"
        f"<td class='num'>{metric_value(row, 'remediated', 'avg')} / {metric_value(row, 'remediated', 'max')}</td>"
        f"<td class='num'><b>{escape(row['change_avg'] or '-')}</b></td><td class='num'><b>{escape(row['change_peak'] or '-')}</b></td>"
        f"<td class='num'>{side(row, 'baseline', 'count') or '-'} / {side(row, 'remediated', 'count') or '-'}</td>"
        f"<td class='muted'>{escape('; '.join(row['notes']))}</td></tr>" for row in p["sampling_comparison"])

    data_json = json.dumps({"baseline": b["flame_graph"], "remediated": r["flame_graph"]}).replace("</", "<\\/")
    body = f"""
<p><a href="/">← all runs</a></p>
<h1>Profiler report · {escape(p['service'])}</h1>
<p class="muted">Categories captured: {escape(', '.join(sorted(set(b['categories']) | set(r['categories']))) or '-')} ·
<a href="/v1/comparison?baseline_run_id={escape(b['run_id'])}&amp;remediated_run_id={escape(r['run_id'])}">raw JSON</a></p>
<div class="grid2">{_run_card(b, 'b')}{_run_card(r, 'r')}</div>
<div class="card summary"><b>Summary (generated from the captured data):</b> {escape(p['summary'])}</div>
{''.join(f'<div class="card warn">{escape(w)}</div>' for w in p['warnings'])}

<h2>Flame graphs (instrumentation spans)</h2>
<div class="card"><label class="toggle"><input type="radio" name="scale" value="fit" checked> Fit each graph to its own width</label>
<label class="toggle"><input type="radio" name="scale" value="shared"> Shared time scale (widths proportional to each run's total)</label>
<div class="muted">Frame width = inclusive duration. Same-name spans under the same parent are merged (hover shows count). Colour: blue = general, orange = cryptographic.</div></div>
<div class="card flame-card phase-card b"><h3><span class="badge baseline">BASELINE</span> {escape(b['label'])} <span class="muted">· {escape(b['run_id'])} · total {_num(b['flame_graph']['value'])} ms</span></h3>
<div id="flame-baseline"></div><div id="details-baseline" class="details"></div></div>
<div class="card flame-card phase-card r"><h3><span class="badge remediated">REMEDIATED</span> {escape(r['label'])} <span class="muted">· {escape(r['run_id'])} · total {_num(r['flame_graph']['value'])} ms</span></h3>
<div id="flame-remediated"></div><div id="details-remediated" class="details"></div></div>

<h2>Span deltas (matched by name + position in tree)</h2>
<div class="card scroll"><table><tr><th>Span</th><th>Status</th><th>Baseline per call</th><th>Remediated per call</th><th>Change per call</th><th>Calls (b / r)</th><th>Total time (b → r)</th><th>Category / attributes / notes</th></tr>
{span_rows or '<tr><td colspan=8 class=muted>No spans captured.</td></tr>'}</table></div>

<h2>Sampling metric deltas (periodic system metrics)</h2>
<div class="card scroll"><table><tr><th>Metric</th><th>Baseline avg / peak</th><th>Remediated avg / peak</th><th>Change (avg)</th><th>Change (peak)</th><th>Samples (b / r)</th><th>Notes</th></tr>
{sample_rows or '<tr><td colspan=7 class=muted>No samples captured.</td></tr>'}</table>
<p class="muted">"Sampling" here means periodic process metrics (psutil), not statistical call-stack sampling.</p></div>

<script type="application/json" id="flame-data">{data_json}</script>
<script>
(function () {{
  const data = JSON.parse(document.getElementById('flame-data').textContent);
  const fmt = v => (v < 10 ? v.toFixed(3) : v.toFixed(1)) + ' ms';
  const color = d => d.data.synthetic_root ? '#9e9e9e' : (d.data.category === 'cryptographic' ? '#e8833a' : d.data.category === 'general' ? '#4a90d9' : '#9b6fd6');
  function label(d) {{
    const a = d.data.attributes || {{}};
    const attrs = Object.keys(a).map(k => k + '=' + a[k].join('/')).join(' ');
    return d.data.name + ' — ' + fmt(d.data.value) + ' total' + (d.data.count > 1 ? ', ×' + d.data.count + ' (mean ' + fmt(d.data.mean_ms) + ')' : '') +
           ', ' + d.data.category + (attrs ? ' [' + attrs + ']' : '');
  }}
  function textTree(node, depth) {{
    return '  '.repeat(depth) + label({{data: node}}) + '\\n' + (node.children || []).map(c => textTree(c, depth + 1)).join('');
  }}
  function draw() {{
    const shared = document.querySelector('input[name=scale]:checked').value === 'shared';
    const max = Math.max(data.baseline.value, data.remediated.value) || 1;
    for (const phase of ['baseline', 'remediated']) {{
      const el = document.getElementById('flame-' + phase);
      el.innerHTML = '';
      if (typeof flamegraph === 'undefined' || typeof d3 === 'undefined') {{
        el.innerHTML = '<div class="warn card">Flame graph library could not be loaded (offline or CDN blocked). Text view of the same tree:</div>' +
                       '<div class="fallback"></div>';
        el.querySelector('.fallback').textContent = textTree(data[phase], 0);
        continue;
      }}
      const full = el.parentElement.clientWidth - 32;
      const width = Math.max(40, Math.round(shared ? full * (data[phase].value / max) : full));
      const chart = flamegraph().width(width).cellHeight(20).transitionDuration(300).minFrameSize(1).sort(false)
        .selfValue(false).setColorMapper(color).setLabelHandler(label)
        .setDetailsElement(document.getElementById('details-' + phase));
      d3.select(el).datum(data[phase]).call(chart);
    }}
  }}
  document.querySelectorAll('input[name=scale]').forEach(i => i.addEventListener('change', draw));
  window.addEventListener('load', draw);
}})();
</script>"""
    head = (f"<link rel='stylesheet' href='{FLAME_CSS}'><script src='{D3}'></script><script src='{FLAME_JS}'></script>")
    return _page(f"Profiler report · {p['service']}", body, head)
