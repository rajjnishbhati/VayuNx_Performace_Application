"use client";

import { fmtSeconds } from "@/lib/format";
import type { Experiment } from "@/lib/types";

const STAGE: Record<string, string> = {
  "quiet-check": "waiting for a quiet machine",
  measuring: "measuring",
  done: "saving",
};

export default function LabProgress({ exp, onCancel }: { exp: Experiment; onCancel: () => void }) {
  const cur = exp.progress.current;
  return (
    <section className="card" aria-labelledby="progress-title">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2 id="progress-title" style={{ margin: 0 }}>Running: {exp.label}</h2>
        <button onClick={onCancel} disabled={exp.status === "cancelling"}>
          {exp.status === "cancelling" ? "Cancelling…" : "Cancel"}
        </button>
      </div>
      <div className="progress" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={exp.progress.percent}
           aria-label="Experiment progress" style={{ margin: "12px 0 8px" }}>
        <div style={{ width: `${exp.progress.percent}%` }} />
      </div>
      <p aria-live="polite" className="ink2" style={{ margin: 0 }}>
        {exp.progress.done} of {exp.progress.total} trials done
        {cur.variant ? ` · now: ${cur.variant}, trial ${cur.trial} of ${cur.trials} (${STAGE[cur.stage ?? ""] ?? cur.stage})` : ""}
        {exp.progress.eta_s != null ? ` · about ${fmtSeconds(exp.progress.eta_s)} left` : ""}
      </p>
      <p className="muted" style={{ fontSize: 12, margin: "6px 0 0" }}>
        Each trial runs in a fresh process; algorithms alternate (A B, B A…) so machine drift spreads evenly. Keep other
        heavy work off this machine meanwhile — noisy trials are flagged.
      </p>
    </section>
  );
}
