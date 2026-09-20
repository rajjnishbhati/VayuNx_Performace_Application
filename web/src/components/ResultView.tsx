"use client";

import { useEffect, useRef, useState } from "react";
import DotPlot from "@/components/charts/DotPlot";
import MachineView, { machineTable } from "@/components/charts/MachineView";
import FlagNote, { flagsFor } from "@/components/FlagNote";
import SecurityBadge from "@/components/SecurityBadge";
import { Skeleton } from "@/components/States";
import Tabs from "@/components/Tabs";
import { api } from "@/lib/api";
import { colorMap } from "@/lib/colors";
import { PRESENT_KEY, applyPresent, useStoredString } from "@/lib/useStored";
import { fmtBytes, fmtCores, fmtDate, fmtInt, fmtNs } from "@/lib/format";
import type { CompareResult, Timeseries, Variant } from "@/lib/types";

function Swatch({ color }: { color: string }) {
  return <span className="swatch" style={{ background: color, display: "inline-block", verticalAlign: "-1px", marginRight: 8 }} aria-hidden />;
}

function ChartTableToggle({ value, onChange }: { value: "chart" | "table"; onChange: (v: "chart" | "table") => void }) {
  return (
    <div className="segmented" role="group" aria-label="Show as" style={{ marginBottom: 12 }}>
      <button aria-pressed={value === "chart"} onClick={() => onChange("chart")}>Chart</button>
      <button aria-pressed={value === "table"} onClick={() => onChange("table")}>Table</button>
    </div>
  );
}

function Scorecard({ r, colors, onReference }: { r: CompareResult; colors: Record<string, string>; onReference?: (k: string) => void }) {
  const ref = r.variants.find((v) => v.is_reference)!;
  const exp = r.experiment;
  return (
    <div className="scroll-x">
      <table className="data">
        <caption className="muted" style={{ captionSide: "bottom", textAlign: "left", fontSize: 12, paddingTop: 8 }}>
          {exp ? `Median of ${exp.params.trials} trials × ${exp.params.duration_s} s per algorithm` : "Median across the selected runs"}
          {exp?.env ? ` on ${exp.env.cpu_model}` : ""}. Change is per call, against {ref.label}.
        </caption>
        <thead>
          <tr>
            <th scope="col">Algorithm</th>
            <th scope="col" className="num">Time per call</th>
            <th scope="col" className="num">CPU cores busy</th>
            <th scope="col" className="num">Peak RAM</th>
            <th scope="col">Safe for passwords</th>
            <th scope="col">Change</th>
          </tr>
        </thead>
        <tbody>
          {r.variants.map((v) => (
            <tr key={v.key}>
              <th scope="row" style={{ fontWeight: 500 }}>
                <Swatch color={colors[v.key]} />{v.label}
                {v.is_reference
                  ? <span className="muted" style={{ fontSize: 12 }}> · reference</span>
                  : onReference && <button className="flag" style={{ color: "var(--focus)" }} onClick={() => onReference(v.key)}>compare against this</button>}
              </th>
              <td className="num">
                <b>{fmtNs(v.time_per_call.median_ns)}</b>
                <div className="muted" style={{ fontSize: 12 }}>p95 {fmtNs(v.time_per_call.p95_ns)}</div>
                <FlagNote flags={flagsFor(v.flags, "time_per_call")} />
              </td>
              <td className="num">{fmtCores(v.cpu.cores_busy)} <FlagNote flags={flagsFor(v.flags, "cores_busy")} /></td>
              <td className="num">
                {v.memory.approximate ? "≈ " : ""}{fmtBytes(v.memory.peak_rss_bytes)} <FlagNote flags={flagsFor(v.flags, "peak_ram")} />
              </td>
              <td><SecurityBadge security={v.security} /></td>
              <td>{v.is_reference ? <span className="muted">reference</span> : v.vs_reference?.time_change}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AppView({ r, colors }: { r: CompareResult; colors: Record<string, string> }) {
  const [mode, setMode] = useState<"chart" | "table">("chart");
  return (
    <div>
      <ChartTableToggle value={mode} onChange={setMode} />
      {mode === "chart" ? <DotPlot variants={r.variants} colors={colors} /> : (
        <div className="scroll-x">
          <table className="data">
            <thead><tr><th scope="col">Algorithm</th><th scope="col" className="num">Median</th><th scope="col" className="num">p95</th>
              <th scope="col" className="num">p99</th><th scope="col" className="num">Mean</th><th scope="col" className="num">IQR</th>
              <th scope="col" className="num">Trial medians (min – max)</th><th scope="col" className="num">95% CI of median</th><th scope="col">Precision</th></tr></thead>
            <tbody>
              {r.variants.map((v) => {
                const t = v.time_per_call;
                return (
                  <tr key={v.key}>
                    <th scope="row" style={{ fontWeight: 500 }}><Swatch color={colors[v.key]} />{v.label}</th>
                    <td className="num">{fmtNs(t.median_ns)}</td><td className="num">{fmtNs(t.p95_ns)}</td><td className="num">{fmtNs(t.p99_ns)}</td>
                    <td className="num">{fmtNs(t.mean_ns)}</td><td className="num">{fmtNs(t.iqr_ns)}</td>
                    <td className="num">{t.range_ns ? `${fmtNs(t.range_ns[0])} – ${fmtNs(t.range_ns[1])}` : "–"}</td>
                    <td className="num">{t.ci95_ns ? `${fmtNs(t.ci95_ns[0])} – ${fmtNs(t.ci95_ns[1])}` : "–"}</td>
                    <td className="muted">{t.percentile_method}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {r.source === "app" && r.operation && (
        <div className="muted" style={{ fontSize: 12 }}>
          <p style={{ margin: "4px 0" }}>
            {r.operation_kind === "span" ? <>Compared: the crypto calls inside <code>{r.operation}</code></> : <>Operation compared: <code>{r.operation}</code></>}
            {r.operation_detail && <> ({Object.entries(r.operation_detail).map(([k, parts]) => `${k}: ${parts.join(" + ")}`).join("; ")})</>}
            {r.other_operations?.length ? `. Also recorded: ${r.other_operations.join(", ")}` : ""}.
          </p>
          {r.measurement_notes?.map((n) => <p key={n} style={{ margin: "4px 0" }}>{n}</p>)}
        </div>
      )}
    </div>
  );
}

function MachineTab({ r, ts, tsLoading, colors }: { r: CompareResult; ts: Timeseries | null; tsLoading: boolean; colors: Record<string, string> }) {
  const [mode, setMode] = useState<"chart" | "table">("chart");
  if (r.source === "app") {
    return (
      <div>
        <p className="ink2">Per variant, from the SDK sampler while crypto was running (approximate):</p>
        <ul>{r.variants.map((v) => <li key={v.key}><Swatch color={colors[v.key]} />{v.label}: {fmtCores(v.cpu.cores_busy)} cores busy, peak ≈ {fmtBytes(v.memory.peak_rss_bytes)}</li>)}</ul>
        <p className="muted" style={{ fontSize: 12 }}>The SDKs send these gauges every second; a time-series chart for app runs is not built yet (the per-run samples are in the CSV/API: GET /v2/runs/&#123;id&#125;/samples).</p>
      </div>
    );
  }
  if (tsLoading) return <div className="skeleton" style={{ height: 360 }} aria-label="Loading machine data" />;
  if (!ts) return <p className="muted">No machine data for this experiment.</p>;
  const rows = machineTable(ts);
  return (
    <div>
      <ChartTableToggle value={mode} onChange={setMode} />
      {mode === "chart" ? <MachineView ts={ts} colors={colors} /> : (
        <div className="scroll-x">
          <table className="data">
            <thead><tr><th scope="col">Algorithm</th><th scope="col" className="num">Trial</th><th scope="col" className="num">Seconds</th>
              <th scope="col" className="num">Cores busy (median)</th><th scope="col" className="num">Peak RSS</th>
              <th scope="col" className="num">Machine CPU (median)</th><th scope="col" className="num">Other work (median)</th><th scope="col">Noise</th></tr></thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={i}>
                  <th scope="row" style={{ fontWeight: 500 }}><Swatch color={colors[row.preset]} />{row.variant}</th>
                  <td className="num">{row.trial}</td><td className="num">{row.seconds.toFixed(1)}</td>
                  <td className="num">{fmtCores(row.cores)}</td>
                  <td className="num">{row.peakRssMiB != null ? fmtBytes(row.peakRssMiB * 1024 * 1024) : "–"}</td>
                  <td className="num">{row.machineCpu != null ? `${row.machineCpu.toFixed(0)}%` : "–"}</td>
                  <td className="num">{fmtCores(row.other)} cores</td>
                  <td>{row.noisy ? <span style={{ color: "var(--warning-ink)" }}>⚠ noisy</span> : "quiet"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function SecurityTab({ r, colors }: { r: CompareResult; colors: Record<string, string> }) {
  const refText = r.variants.find((v) => v.security)?.security?.reference;
  return (
    <div className="stack">
      {r.variants.map((v) => (
        <div key={v.key} className="card" style={{ margin: 0 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <h3 style={{ margin: 0 }}><Swatch color={colors[v.key]} />{v.label}</h3>
            <SecurityBadge security={v.security} />
          </div>
          {v.security ? (
            <>
              <p style={{ marginTop: 8 }}>{v.security.summary}</p>
              <p className="muted" style={{ fontSize: 13, margin: 0 }}>
                Parameters: <code>{v.security.params || "none"}</code> · Meets the OWASP minimum:{" "}
                {v.security.meets_owasp_minimum == null ? "not checked" : v.security.meets_owasp_minimum ? "yes" : "no"}
              </p>
            </>
          ) : <p className="muted">No security reference is known for this algorithm.</p>}
        </div>
      ))}
      {refText && <p className="muted" style={{ fontSize: 12 }}>Source: {refText}.</p>}
    </div>
  );
}

function WhatIf({ r, colors }: { r: CompareResult; colors: Record<string, string> }) {
  const [rate, setRate] = useState(r.capacity_defaults.rate_per_s);
  const [cores, setCores] = useState(r.capacity_defaults.cores_total);
  const ref = r.variants.find((v) => v.is_reference)!;
  const noun = r.source === "lab" ? "login" : "call";
  const line = (v: Variant) => {
    const need = v.cpu.cpu_s_per_op != null ? rate * v.cpu.cpu_s_per_op : null;
    const lat = v.time_per_call.median_ns;
    const ram = lat != null && v.memory.mem_per_op_bytes != null ? rate * (lat / 1e9) * v.memory.mem_per_op_bytes : null;
    const added = !v.is_reference && lat != null && ref.time_per_call.median_ns != null ? lat - ref.time_per_call.median_ns : null;
    return (
      <li key={v.key} style={{ marginBottom: 6 }}>
        <Swatch color={colors[v.key]} /><b>{v.label}</b>:{" "}
        {need != null ? <>{fmtCores(need)} of {cores} cores ({Math.round((need / cores) * 100)}% of the machine)</> : "CPU per call unknown"}
        {ram != null && <> · {fmtBytes(ram)} RAM in flight</>}
        {added != null && <> · {added >= 0 ? "+" : "−"}{fmtNs(Math.abs(added))} per {noun}</>}
        {need != null && need > cores && <span className="muted"> — more CPU than this machine has</span>}
      </li>
    );
  };
  return (
    <section className="card" aria-labelledby="whatif-title">
      <h2 id="whatif-title">What if…</h2>
      <div className="row" style={{ gap: 16, marginBottom: 10 }}>
        <label className="field">{noun === "login" ? "Logins" : "Calls"} per second
          <input type="number" min={1} value={rate} onChange={(e) => setRate(Math.max(0, Number(e.target.value)))} style={{ width: 120 }} />
        </label>
        <label className="field">Cores available
          <input type="number" min={1} value={cores} onChange={(e) => setCores(Math.max(1, Number(e.target.value)))} style={{ width: 100 }} />
        </label>
      </div>
      <ul style={{ margin: 0, paddingLeft: 18 }} aria-live="polite">{r.variants.map(line)}</ul>
      <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>
        Estimate: cores = rate × CPU-seconds per call; RAM in flight = rate × time per call × memory per call. Assumes the
        work spreads evenly over the cores and nothing else runs.
      </p>
    </section>
  );
}

function Details({ r }: { r: CompareResult }) {
  const exp = r.experiment;
  const runs = r.runs ?? [];
  const pair = runs.length === 2 && runs[0].phase === "baseline" && runs[1].phase === "remediated";
  return (
    <details className="card drawer">
      <summary>Details: IDs, machine, per-trial data, method</summary>
      <div className="stack" style={{ marginTop: 12 }}>
        {exp && (
          <div>
            <h3>Experiment</h3>
            <p className="mono">{exp.experiment_id}</p>
            <p className="muted" style={{ fontSize: 13 }}>
              {fmtDate(exp.started_at)} → {fmtDate(exp.completed_at)} · {exp.params.trials} trials × {exp.params.duration_s} s ·
              concurrency {exp.params.concurrency} · status {exp.status}
            </p>
          </div>
        )}
        {exp?.env && (
          <div>
            <h3>Machine (environment fingerprint {exp.env.id})</h3>
            <p style={{ fontSize: 13 }}>
              {exp.env.cpu_model} · {exp.env.cpu_count_physical} cores / {exp.env.cpu_count_logical} threads ·{" "}
              {fmtBytes(exp.env.ram_total_bytes)} RAM · {exp.env.os} · {exp.env.runtime} · {exp.env.openssl} ·{" "}
              {Object.entries(exp.env.libraries).map(([k, v]) => `${k} ${v ?? "–"}`).join(", ")}
            </p>
          </div>
        )}
        {runs.length > 0 && (
          <div>
            <h3>Runs</h3>
            <ul className="mono">{runs.map((x) => <li key={x.run_id}>{x.run_id} · {x.service} · {x.variant ?? x.label}</li>)}</ul>
            {pair && (
              <p><a href={`/api/report?baseline_run_id=${runs[0].run_id}&remediated_run_id=${runs[1].run_id}`}>Open the flame graphs for these two runs</a></p>
            )}
          </div>
        )}
        <div>
          <h3>Per trial</h3>
          <div className="scroll-x">
            <table className="data">
              <thead><tr><th scope="col">Algorithm</th><th scope="col" className="num">Trial</th><th scope="col" className="num">Operations</th>
                <th scope="col" className="num">Median</th><th scope="col" className="num">p95</th><th scope="col">Noise</th></tr></thead>
              <tbody>
                {r.variants.flatMap((v) => v.per_trial.map((t) => (
                  <tr key={`${v.key}-${t.trial_index}`}>
                    <td>{v.label}</td><td className="num">{t.trial_index + 1}</td><td className="num">{fmtInt(t.ops)}</td>
                    <td className="num">{fmtNs(t.p50)}</td><td className="num">{fmtNs(t.p95)}</td>
                    <td>{t.noisy ? "⚠ noisy" : "quiet"}</td>
                  </tr>
                )))}
              </tbody>
            </table>
          </div>
        </div>
        <div>
          <h3>Method</h3>
          <ul style={{ fontSize: 13 }}>{Object.entries(r.method).map(([k, v]) => <li key={k}><b>{k.replace(/_/g, " ")}:</b> {v}</li>)}</ul>
        </div>
      </div>
    </details>
  );
}

/** Downloads of exactly this comparison (same parameters as the screen), and a share link. */
function Exports({ r, sharedToken }: { r: CompareResult; sharedToken?: string }) {
  const [days, setDays] = useState(7);
  const [link, setLink] = useState<string | null>(null);
  const [shareError, setShareError] = useState<string | null>(null);
  if (sharedToken) {
    const base = `/api/v2/shared/${encodeURIComponent(sharedToken)}`;
    return (
      <div className="row" style={{ gap: 12, fontSize: 13, marginTop: 8 }} aria-label="Download this comparison">
        <span className="muted">Download:</span>
        <a href={`${base}.pdf`} download>PDF report</a>
        <a href={`${base}.csv`} download>Scorecard (CSV)</a>
      </div>
    );
  }
  const q = new URLSearchParams();
  if (r.experiment) q.set("experiment_id", r.experiment.experiment_id);
  else if (r.runs) q.set("run_ids", r.runs.map((x) => x.run_id).join(","));
  q.set("reference", r.reference);
  const href = (p: string, extra = "") => `/api/v2/${p}?${q}${extra}`;
  const share = () => {
    setShareError(null);
    api.share({ experiment_id: r.experiment?.experiment_id, run_ids: r.experiment ? undefined : r.runs?.map((x) => x.run_id),
      reference: r.reference, expires_days: days })
      .then((s) => setLink(`${window.location.origin}${s.url}`))
      .catch((e) => setShareError(e.message));
  };
  return (
    <div style={{ marginTop: 8 }}>
      <div className="row" style={{ gap: 12, fontSize: 13 }} aria-label="Download or share this comparison">
        <span className="muted">Download:</span>
        <a href={href("compare.pdf")} download>PDF report</a>
        <a href={href("compare.csv")} download>Scorecard (CSV)</a>
        <a href={href("compare.csv", "&kind=trials")} download>Per-trial data (CSV)</a>
        <span className="spacer" style={{ flex: 1 }} />
        <label className="muted">Share for
          <select aria-label="Share link lifetime" value={days} onChange={(e) => setDays(Number(e.target.value))} style={{ marginLeft: 6 }}>
            <option value={7}>7 days</option><option value={30}>30 days</option><option value={90}>90 days</option>
          </select>
        </label>
        <button onClick={share}>Create share link</button>
      </div>
      {shareError && <p role="alert" style={{ color: "var(--critical-ink)", fontSize: 13 }}>{shareError}</p>}
      {link && (
        <div role="status" className="card" style={{ background: "var(--surface-2)", marginTop: 8 }}>
          <p style={{ margin: "0 0 6px", fontSize: 13 }}>
            Anyone with this link can see this comparison (read-only) for {days} days, without signing in. It is shown only now.
          </p>
          <pre className="code-block" style={{ userSelect: "all" }}>{link}</pre>
          <div className="row" style={{ gap: 8 }}>
            <button onClick={() => { navigator.clipboard?.writeText(link); }}>Copy</button>
            <button onClick={() => setLink(null)}>Done</button>
          </div>
        </div>
      )}
    </div>
  );
}

/** The demo path through a result: the headline first, then the evidence, then what it costs at your traffic.
 *  Each step says in plain words what the audience is looking at; the numbers stay on screen underneath. */
const STEPS = [
  { id: "headline", label: "Headline", caption: "What the switch changes per call", tab: null, target: "verdict" },
  { id: "app", label: "In the code", caption: "Time per call, measured where the code runs", tab: "app", target: "tabs" },
  { id: "machine", label: "On the machine", caption: "CPU and memory while it ran", tab: "machine", target: "tabs" },
  { id: "security", label: "Security", caption: "Is it safe for storing passwords?", tab: "security", target: "tabs" },
  { id: "capacity", label: "At your traffic", caption: "Cores and memory at the rate you choose", tab: null, target: "whatif" },
] as const;

function PresentBar({ step, count, onStep, onExit }: { step: number; count: number; onStep: (i: number) => void; onExit: () => void }) {
  const s = STEPS[step];
  return (
    <div className="present-bar" role="toolbar" aria-label="Presentation steps">
      <span className="step">{step + 1}/{count} · {s.label}</span>
      <span className="caption">{s.caption}</span>
      <button onClick={() => onStep(step - 1)} disabled={step === 0} aria-label="Previous step">◀ Back</button>
      <button onClick={() => onStep(step + 1)} disabled={step === count - 1} aria-label="Next step">Next ▶</button>
      <button onClick={onExit}>Exit</button>
    </div>
  );
}

export default function ResultView({ result, ts, tsLoading, onReference, sharedToken }: {
  result: CompareResult; ts: Timeseries | null; tsLoading: boolean; onReference?: (key: string) => void;
  /** set on /shared/<token>: read-only, downloads go through the link */
  sharedToken?: string;
}) {
  const [tab, setTab] = useState("app");
  const present = useStoredString(PRESENT_KEY, "off") === "on";
  const [step, setStep] = useState(0);
  const verdictRef = useRef<HTMLElement>(null);
  const tabsRef = useRef<HTMLElement>(null);
  const whatifRef = useRef<HTMLDivElement>(null);

  /** Move to a step: switch the tab it needs, then bring that part of the page into view. */
  const go = (i: number) => {
    const n = Math.max(0, Math.min(STEPS.length - 1, i));
    const s = STEPS[n];
    setStep(n);
    if (s.tab) setTab(s.tab);
    const el = s.target === "verdict" ? verdictRef : s.target === "tabs" ? tabsRef : whatifRef;
    const smooth = !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    requestAnimationFrame(() => el.current?.scrollIntoView({ behavior: smooth ? "smooth" : "auto", block: "start" }));
  };

  // Entering presentation mode starts the audience at the current step rather than wherever the page was scrolled.
  useEffect(() => {
    if (!present) return;
    const s = STEPS[step];
    const el = s.target === "verdict" ? verdictRef : s.target === "tabs" ? tabsRef : whatifRef;
    requestAnimationFrame(() => el.current?.scrollIntoView({ block: "start" }));
  }, [present]); // eslint-disable-line react-hooks/exhaustive-deps -- only on entering, not on every step

  useEffect(() => {
    if (!present) return;
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && /^(INPUT|SELECT|TEXTAREA)$/.test(el.tagName)) return; // typing in the what-if boxes
      if (e.key === "ArrowRight" || e.key === "PageDown") go(step + 1);
      else if (e.key === "ArrowLeft" || e.key === "PageUp") go(step - 1);
      else if (e.key === "Escape") applyPresent(false);
      else return;
      e.preventDefault();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  });  // re-bound each render, so the handler always sees the current step

  const order = result.experiment?.params.presets ?? result.variants.map((v) => v.key);
  const colors = colorMap(order, result.reference);
  return (
    <div>
      <section className="card present-target" ref={verdictRef} aria-labelledby="verdict-title">
        <h2 id="verdict-title" className="sr-only">Result</h2>
        <p className="verdict" aria-live="polite">{result.verdict}</p>
        <Scorecard r={result} colors={colors} onReference={onReference} />
        <Exports r={result} sharedToken={sharedToken} />
      </section>
      <section className="card present-target" ref={tabsRef}>
        <Tabs active={tab} onChange={setTab} tabs={[
          { id: "app", label: "App view (instrumentation)", content: <AppView r={result} colors={colors} /> },
          { id: "machine", label: "Machine view (sampling)", content: <MachineTab r={result} ts={ts} tsLoading={tsLoading} colors={colors} /> },
          { id: "security", label: "Security", content: <SecurityTab r={result} colors={colors} /> },
        ]} />
      </section>
      <div className="present-target" ref={whatifRef}>
        <WhatIf key={result.reference} r={result} colors={colors} />
      </div>
      <Details r={result} />
      {present && <PresentBar step={step} count={STEPS.length} onStep={go} onExit={() => applyPresent(false)} />}
    </div>
  );
}

export { Skeleton };
