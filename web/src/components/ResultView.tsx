"use client";

import { useState } from "react";
import DotPlot from "@/components/charts/DotPlot";
import MachineView, { machineTable } from "@/components/charts/MachineView";
import FlagNote, { flagsFor } from "@/components/FlagNote";
import SecurityBadge from "@/components/SecurityBadge";
import { Skeleton } from "@/components/States";
import Tabs from "@/components/Tabs";
import { colorMap } from "@/lib/colors";
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
        <p className="muted" style={{ fontSize: 12 }}>Operation compared: <code>{r.operation}</code>{r.other_operations?.length ? ` (also recorded: ${r.other_operations.join(", ")})` : ""}.</p>
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
        <p className="muted" style={{ fontSize: 12 }}>A time-series view of app runs comes with the Phase 3 SDKs.</p>
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

export default function ResultView({ result, ts, tsLoading, onReference }: {
  result: CompareResult; ts: Timeseries | null; tsLoading: boolean; onReference?: (key: string) => void;
}) {
  const [tab, setTab] = useState("app");
  const order = result.experiment?.params.presets ?? result.variants.map((v) => v.key);
  const colors = colorMap(order, result.reference);
  return (
    <div>
      <section className="card" aria-labelledby="verdict-title">
        <h2 id="verdict-title" className="sr-only">Result</h2>
        <p className="verdict" aria-live="polite">{result.verdict}</p>
        <Scorecard r={result} colors={colors} onReference={onReference} />
      </section>
      <section className="card">
        <Tabs active={tab} onChange={setTab} tabs={[
          { id: "app", label: "App view (instrumentation)", content: <AppView r={result} colors={colors} /> },
          { id: "machine", label: "Machine view (sampling)", content: <MachineTab r={result} ts={ts} tsLoading={tsLoading} colors={colors} /> },
          { id: "security", label: "Security", content: <SecurityTab r={result} colors={colors} /> },
        ]} />
      </section>
      <WhatIf key={result.reference} r={result} colors={colors} />
      <Details r={result} />
    </div>
  );
}

export { Skeleton };
