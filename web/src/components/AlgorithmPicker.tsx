"use client";

import SecurityBadge from "@/components/SecurityBadge";
import { MAX_ALGORITHMS, colorMap } from "@/lib/colors";
import { fmtSeconds } from "@/lib/format";
import type { Preset } from "@/lib/types";

export const QUICK_PICKS: { label: string; presets: string[] }[] = [
  { label: "MD5 → Argon2id", presets: ["md5", "argon2id-rfc9106-low"] },
  { label: "MD5 → bcrypt 12", presets: ["md5", "bcrypt-12"] },
  { label: "SHA-256 → Argon2id (OWASP min)", presets: ["sha256", "argon2id-owasp"] },
  { label: "Password hashes side by side", presets: ["argon2id-owasp", "bcrypt-10", "scrypt-n17", "pbkdf2-sha256-600k"] },
];

type Props = {
  presets: Preset[];
  selection: string[];
  reference: string | null;
  trials: number;
  durationS: number;
  running: boolean;
  onSelection: (sel: string[], reference?: string) => void;
  onReference: (ref: string) => void;
  onTrials: (n: number) => void;
  onDuration: (s: number) => void;
  onRun: () => void;
  onQuickRun: (presets: string[]) => void;
};

export default function AlgorithmPicker(p: Props) {
  const ref = p.reference ?? p.selection[0] ?? null;
  const colors = colorMap(p.selection, ref ?? "");
  const estimate = p.selection.length * p.trials * (p.durationS + 2.5);
  const toggle = (id: string) => {
    if (p.selection.includes(id)) p.onSelection(p.selection.filter((s) => s !== id));
    else if (p.selection.length < MAX_ALGORITHMS) p.onSelection([...p.selection, id]);
  };
  return (
    <section className="card" aria-labelledby="pick-title">
      <h2 id="pick-title">Which algorithms?</h2>
      <div className="row" style={{ marginBottom: 12 }} role="group" aria-label="Run a common comparison now">
        <span className="muted" style={{ fontSize: 13 }}>Run now:</span>
        {QUICK_PICKS.map((q) => (
          <button key={q.label} onClick={() => p.onQuickRun(q.presets)} disabled={p.running}
                  title={`Start ${q.label}: ${p.trials} trials × ${p.durationS} s each (you can cancel)`}>
            <span aria-hidden>▶ </span>{q.label}
          </button>
        ))}
      </div>
      <p className="muted" style={{ fontSize: 13, margin: "0 0 8px" }}>…or choose your own:</p>
      <div className="row" role="group" aria-label={`Algorithms (pick 2 to ${MAX_ALGORITHMS})`}>
        {p.presets.map((pr) => {
          const on = p.selection.includes(pr.id);
          return (
            <button key={pr.id} className="chip" aria-pressed={on} onClick={() => toggle(pr.id)}
                    disabled={p.running || (!on && p.selection.length >= MAX_ALGORITHMS)}
                    title={`${pr.label} · ${pr.library}`}>
              <span className={on ? "swatch" : "swatch empty"} style={on ? { background: colors[pr.id] } : undefined} aria-hidden />
              <span>{pr.label}</span>
              <SecurityBadge security={pr.security} />
            </button>
          );
        })}
      </div>
      <p className="muted" style={{ fontSize: 12, margin: "8px 0 0" }}>
        Up to {MAX_ALGORITHMS}. The reference is grey; the chips double as the chart legend.
      </p>
      <div className="row" style={{ marginTop: 14, alignItems: "end", gap: 16 }}>
        <label className="field">
          Compare against
          <select value={ref ?? ""} onChange={(e) => p.onReference(e.target.value)} disabled={p.running || p.selection.length < 2}>
            {p.selection.map((id) => <option key={id} value={id}>{p.presets.find((x) => x.id === id)?.label ?? id}</option>)}
          </select>
        </label>
        <details>
          <summary className="muted" style={{ cursor: "pointer", fontSize: 13 }}>Trials: {p.trials} × {p.durationS} s</summary>
          <div className="row" style={{ marginTop: 8, gap: 12 }}>
            <label className="field">Trials per algorithm
              <input type="number" min={1} max={20} value={p.trials} onChange={(e) => p.onTrials(Number(e.target.value))} style={{ width: 90 }} />
            </label>
            <label className="field">Seconds per trial
              <input type="number" min={1} max={60} value={p.durationS} onChange={(e) => p.onDuration(Number(e.target.value))} style={{ width: 90 }} />
            </label>
          </div>
          {p.trials < 5 && <p style={{ color: "var(--warning-ink)", fontSize: 12 }}>Fewer than 5 trials: results will be flagged as weak data.</p>}
        </details>
        <span className="spacer" style={{ flex: 1 }} />
        <span className="muted" style={{ fontSize: 13 }}>{p.selection.length >= 2 ? `about ${fmtSeconds(estimate)}` : ""}</span>
        <button className="btn-primary" onClick={p.onRun} disabled={p.running || p.selection.length < 2}>
          {p.running ? "Running…" : "Run"}
        </button>
      </div>
    </section>
  );
}
