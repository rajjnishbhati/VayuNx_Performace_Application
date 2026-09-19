"use client";

import { useCallback, useEffect, useState } from "react";
import { ErrorState } from "@/components/States";
import { ApiError, api } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import type { RunItem } from "@/lib/types";

/** "My app" source: pick instrumented app runs (two or more variants) and compare them. */
export default function AppRunsPicker({ onCompare }: { onCompare: (runIds: string[]) => void }) {
  const [runs, setRuns] = useState<RunItem[] | null>(null);
  const [err, setErr] = useState<ApiError | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const load = useCallback(() => {
    api.runs("?source=app&limit=50").then((p) => setRuns(p.items)).catch((e) => setErr(e));
  }, []);
  useEffect(() => { load(); }, [load]);

  if (err) return <ErrorState error={err.message} fix={err.fix} onRetry={() => { setErr(null); load(); }} />;
  if (!runs) return <div className="card"><div className="skeleton" style={{ height: 80 }} /></div>;
  if (!runs.length) {
    return (
      <div className="card">
        <h2>No app runs yet</h2>
        <p className="ink2">
          Instrument your app with the Python SDK (<code>run.op()</code> around crypto calls, with <code>crypto.operation</code> and{" "}
          <code>crypto.algorithm</code> attributes), run it once per variant, and the runs appear here. Automatic hooks and the
          Next.js SDK arrive in Phase 3.
        </p>
      </div>
    );
  }
  const toggle = (id: string) => setPicked((p) => (p.includes(id) ? p.filter((x) => x !== id) : [...p, id]));
  return (
    <section className="card" aria-labelledby="apps-title">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 id="apps-title" style={{ margin: 0 }}>Pick app runs to compare</h2>
        <button className="btn-primary" disabled={picked.length < 2} onClick={() => onCompare(picked)}>
          Compare {picked.length || ""} runs
        </button>
      </div>
      <p className="muted" style={{ fontSize: 13 }}>Runs with the same variant name count as trials of that variant; the first picked is the reference.</p>
      <div className="scroll-x">
        <table className="data">
          <thead><tr><th scope="col"><span className="sr-only">Select</span></th><th scope="col">Service</th><th scope="col">Variant</th><th scope="col">Recorded</th><th scope="col" className="num">Summaries</th><th scope="col" className="num">Spans</th></tr></thead>
          <tbody>
            {runs.map((r) => (
              <tr key={r.run_id}>
                <td><input type="checkbox" aria-label={`Select ${r.service} ${r.variant ?? r.label}`} checked={picked.includes(r.run_id)} onChange={() => toggle(r.run_id)} /></td>
                <td>{r.service}</td>
                <td>{r.variant ?? r.label}</td>
                <td className="muted">{fmtDate(r.created_at)}</td>
                <td className="num">{r.op_stat_count}</td>
                <td className="num">{r.span_count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
