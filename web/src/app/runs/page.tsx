"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ErrorState } from "@/components/States";
import { ApiError, api } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import type { Experiment, Page, RunItem } from "@/lib/types";

const PAGE_SIZE = 25;

export default function RunsPage() {
  const router = useRouter();
  const [q, setQ] = useState("");
  const [source, setSource] = useState("");
  const [service, setService] = useState("");
  const [algorithm, setAlgorithm] = useState("");
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [offset, setOffset] = useState(0);
  const [data, setData] = useState<Page<RunItem> | null>(null);
  const [exps, setExps] = useState<Experiment[]>([]);
  const [picked, setPicked] = useState<RunItem[]>([]);
  const [error, setError] = useState<ApiError | null>(null);

  const load = useCallback(() => {
    const qs = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (q) qs.set("q", q);
    if (source) qs.set("source", source);
    if (service) qs.set("service", service);
    if (algorithm) qs.set("algorithm", algorithm);
    if (from) qs.set("date_from", new Date(from).toISOString());
    if (to) qs.set("date_to", new Date(`${to}T23:59:59`).toISOString());
    setError(null);
    api.runs(`?${qs}`).then(setData).catch(setError);
    api.experiments("?limit=10").then((p) => setExps(p.items)).catch(() => undefined);
  }, [q, source, service, algorithm, from, to, offset]);

  useEffect(() => {
    const t = setTimeout(load, 250); // debounce typing
    return () => clearTimeout(t);
  }, [load]);

  const toggle = (r: RunItem) =>
    setPicked((p) => (p.some((x) => x.run_id === r.run_id) ? p.filter((x) => x.run_id !== r.run_id) : [...p, r]));

  const compareTarget = (() => {
    if (picked.length < 2) return null;
    const expIds = new Set(picked.map((r) => r.experiment_id));
    if (picked.every((r) => r.source === "lab") && expIds.size === 1) return `/?experiment=${picked[0].experiment_id}`;
    if (picked.every((r) => r.source === "app")) return `/?runs=${picked.map((r) => r.run_id).join(",")}`;
    return null;
  })();

  return (
    <div className="page">
      <h1>Runs</h1>
      <p className="muted">Search every recorded run. Select two or more to compare them.</p>

      {exps.length > 0 && (
        <section className="card" aria-labelledby="exp-title">
          <h2 id="exp-title">Recent Lab experiments</h2>
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {exps.map((e) => (
              <li key={e.experiment_id}>
                <a href={`/?experiment=${e.experiment_id}`}>{e.label}</a>{" "}
                <span className="muted">· {e.status} · {e.params.trials} × {e.params.duration_s} s · {fmtDate(e.created_at)}</span>
              </li>
            ))}
          </ul>
        </section>
      )}

      <section className="card" aria-label="Filters">
        <div className="row" style={{ gap: 12, alignItems: "end" }}>
          <label className="field">Search<input type="search" value={q} placeholder="label, variant or service" onChange={(e) => { setOffset(0); setQ(e.target.value); }} /></label>
          <label className="field">Source
            <select value={source} onChange={(e) => { setOffset(0); setSource(e.target.value); }}>
              <option value="">All</option><option value="lab">Lab test</option><option value="app">My app</option>
            </select>
          </label>
          <label className="field">Service<input value={service} onChange={(e) => { setOffset(0); setService(e.target.value); }} style={{ width: 150 }} /></label>
          <label className="field">Algorithm<input value={algorithm} onChange={(e) => { setOffset(0); setAlgorithm(e.target.value); }} style={{ width: 150 }} /></label>
          <label className="field">From<input type="date" value={from} onChange={(e) => { setOffset(0); setFrom(e.target.value); }} /></label>
          <label className="field">To<input type="date" value={to} onChange={(e) => { setOffset(0); setTo(e.target.value); }} /></label>
        </div>
      </section>

      {error ? <ErrorState error={error.message} fix={error.fix} onRetry={load} /> : (
        <section className="card" aria-label="Runs">
          <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
            <span className="muted">{data ? `${data.total} runs` : "Loading…"}{picked.length ? ` · ${picked.length} selected` : ""}</span>
            <button className="btn-primary" disabled={!compareTarget} onClick={() => compareTarget && router.push(compareTarget)}
                    title={picked.length >= 2 && !compareTarget ? "Pick Lab trials from one experiment, or app runs only" : undefined}>
              Compare selected
            </button>
          </div>
          {picked.length >= 2 && !compareTarget && (
            <p style={{ color: "var(--warning-ink)", fontSize: 13 }}>Pick Lab trials from one experiment, or app runs only.</p>
          )}
          <div className="scroll-x">
            <table className="data" style={{ opacity: data ? 1 : 0.5 }}>
              <thead><tr><th scope="col"><span className="sr-only">Select</span></th><th scope="col">Source</th><th scope="col">Service</th>
                <th scope="col">Algorithm / variant</th><th scope="col" className="num">Trial</th><th scope="col">Recorded</th></tr></thead>
              <tbody>
                {data?.items.map((r) => (
                  <tr key={r.run_id}>
                    <td><input type="checkbox" aria-label={`Select ${r.variant ?? r.label}`} checked={picked.some((x) => x.run_id === r.run_id)} onChange={() => toggle(r)} /></td>
                    <td>{r.source === "lab" ? "Lab test" : "My app"}</td>
                    <td>{r.service}</td>
                    <td>{r.variant ?? r.label}</td>
                    <td className="num">{r.trial_index != null ? r.trial_index + 1 : "–"}</td>
                    <td className="muted">{fmtDate(r.created_at)}</td>
                  </tr>
                ))}
                {data && !data.items.length && <tr><td colSpan={6} className="muted">No runs match these filters.</td></tr>}
              </tbody>
            </table>
          </div>
          {data && data.total > PAGE_SIZE && (
            <div className="row" style={{ marginTop: 10 }}>
              <button disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>Previous</button>
              <span className="muted">{offset + 1}–{Math.min(offset + PAGE_SIZE, data.total)} of {data.total}</span>
              <button disabled={offset + PAGE_SIZE >= data.total} onClick={() => setOffset(offset + PAGE_SIZE)}>Next</button>
            </div>
          )}
        </section>
      )}
    </div>
  );
}
