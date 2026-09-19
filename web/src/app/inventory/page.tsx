"use client";

import { useCallback, useEffect, useState } from "react";
import { ErrorState, Skeleton } from "@/components/States";
import { ApiError, api, currentProject } from "@/lib/api";
import { fmtDate } from "@/lib/format";
import type { Inventory, InventoryItem } from "@/lib/types";

/** Status of one algorithm for password storage: always an icon and a label, never colour alone. */
function Status({ item }: { item: InventoryItem }) {
  let icon = "–", label = "Not checked", color = "var(--muted)";
  if (item.safe_for_passwords === false) { icon = "✕"; label = "Not for passwords"; color = "var(--critical-ink)"; }
  else if (item.meets_owasp_minimum === false) { icon = "⚠"; label = "Below OWASP minimum"; color = "var(--warning-ink)"; }
  else if (item.meets_owasp_minimum === true) { icon = "✓"; label = "Meets OWASP minimum"; color = "var(--good-ink)"; }
  return (
    <span title={item.owasp_note} style={{ color, whiteSpace: "nowrap" }}>
      <span aria-hidden>{icon} </span>{label}
    </span>
  );
}

export default function InventoryPage() {
  const [inv, setInv] = useState<Inventory | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const load = useCallback(() => { api.inventory().then(setInv).catch(setError); }, []);
  useEffect(() => { load(); }, [load]);
  const project = currentProject();
  return (
    <div className="page">
      <h1>Crypto inventory</h1>
      <p className="muted">
        Which algorithms each app was seen using, from its measured runs (Lab benchmarks are not included). Password hashes
        are checked against the OWASP minimums when their parameters were recorded.{" "}
        <a href={`/api/v2/inventory.csv${project ? `?project=${encodeURIComponent(project)}` : ""}`} download>Download (CSV)</a>
      </p>
      {error ? <ErrorState error={error.message} fix={error.fix} onRetry={() => { setError(null); load(); }} /> : !inv ? <Skeleton lines={5} /> : (
        inv.apps.length === 0 ? (
          <section className="card"><h2>No app data yet</h2><p className="ink2">Run an app under the profiler (see Compare → My app) and it appears here.</p></section>
        ) : inv.apps.map((app) => (
          <section key={app.service} className="card" aria-labelledby={`inv-${app.service}`}>
            <h2 id={`inv-${app.service}`} style={{ marginBottom: 4 }}>{app.service}</h2>
            <p className="muted" style={{ fontSize: 13, marginTop: 0 }}>
              {app.summary.algorithms} algorithm{app.summary.algorithms === 1 ? "" : "s"} seen
              {app.summary.not_for_passwords ? ` · ${app.summary.not_for_passwords} not for passwords` : ""}
              {app.summary.below_owasp_minimum ? ` · ${app.summary.below_owasp_minimum} below the OWASP minimum` : ""}
            </p>
            <div className="scroll-x">
              <table className="data">
                <thead>
                  <tr><th scope="col">Algorithm</th><th scope="col">Used for</th><th scope="col">Parameters</th>
                    <th scope="col">Library · runtime</th><th scope="col">Where</th><th scope="col" className="num">Calls</th>
                    <th scope="col">Last seen</th><th scope="col">Passwords</th></tr>
                </thead>
                <tbody>
                  {app.algorithms.map((i) => (
                    <tr key={i.key}>
                      <th scope="row" style={{ fontWeight: 500 }}>{i.algorithm}</th>
                      <td>{i.operation}</td>
                      <td className="mono">{i.params ?? "–"}</td>
                      <td className="muted" style={{ fontSize: 13 }}>{i.library ?? "–"}{i.runtime ? ` · ${i.runtime}` : ""}</td>
                      <td>{i.scopes.length ? i.scopes.map((s) => <code key={s} style={{ marginRight: 6 }}>{s}</code>) : <span className="muted">outside named code paths</span>}</td>
                      <td className="num">{i.calls.toLocaleString()}<div className="muted" style={{ fontSize: 12 }}>{i.runs} run{i.runs === 1 ? "" : "s"}</div></td>
                      <td className="muted">{fmtDate(i.last_seen)}</td>
                      <td><Status item={i} /><div className="muted" style={{ fontSize: 12, maxWidth: 320 }}>{i.owasp_note}</div></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        ))
      )}
      {inv && <p className="muted" style={{ fontSize: 12 }}>{inv.note}</p>}
    </div>
  );
}
