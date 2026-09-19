"use client";

import { useCallback, useEffect, useState } from "react";
import SecurityBadge from "@/components/SecurityBadge";
import { ErrorState } from "@/components/States";
import { ApiError, api } from "@/lib/api";
import type { Preset } from "@/lib/types";

export default function AlgorithmsPage() {
  const [presets, setPresets] = useState<Preset[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const load = useCallback(() => { api.presets().then(setPresets).catch(setError); }, []);
  useEffect(() => { load(); }, [load]);
  return (
    <div className="page">
      <h1>Algorithms</h1>
      <p className="muted">Built-in Lab presets. The Lab can only run these, with these parameters.</p>
      {error ? <ErrorState error={error.message} fix={error.fix} onRetry={() => { setError(null); load(); }} /> : (
        <section className="card">
          <div className="scroll-x">
            <table className="data">
              <thead><tr><th scope="col">Preset</th><th scope="col">Parameters</th><th scope="col">Library</th><th scope="col">Passwords</th><th scope="col">Security note</th></tr></thead>
              <tbody>
                {presets?.map((p) => (
                  <tr key={p.id}>
                    <th scope="row" style={{ fontWeight: 500 }}>{p.label}<div className="muted mono">{p.id}</div></th>
                    <td className="mono">{p.params || "–"}</td>
                    <td className="muted" style={{ fontSize: 13 }}>{p.library}</td>
                    <td><SecurityBadge security={p.security} /></td>
                    <td style={{ fontSize: 13 }}>{p.security.summary}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {presets?.[0] && <p className="muted" style={{ fontSize: 12, marginTop: 8 }}>Source: {presets[0].security.reference}.</p>}
        </section>
      )}
    </div>
  );
}
