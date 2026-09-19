"use client";

import { useState } from "react";
import { useStoredNumber, writeStored } from "@/lib/useStored";

export default function SettingsPage() {
  const storedTrials = useStoredNumber("vx-trials", 5);
  const storedDuration = useStoredNumber("vx-duration", 10);
  const [trials, setTrials] = useState<number | null>(null); // null = not edited yet: show the stored value
  const [duration, setDuration] = useState<number | null>(null);
  const [saved, setSaved] = useState(false);
  const t = trials ?? storedTrials;
  const d = duration ?? storedDuration;
  const save = () => {
    writeStored("vx-trials", String(t));
    writeStored("vx-duration", String(d));
    setSaved(true);
  };
  return (
    <div className="page">
      <h1>Settings</h1>
      <section className="card">
        <h2>Lab defaults</h2>
        <p className="muted">Used for new comparisons in this browser. Five trials of 10 seconds is the minimum for trustworthy spreads.</p>
        <div className="row" style={{ gap: 16, alignItems: "end" }}>
          <label className="field">Trials per algorithm
            <input type="number" min={1} max={20} value={t} onChange={(e) => { setSaved(false); setTrials(Math.min(20, Math.max(1, Number(e.target.value) || 1))); }} />
          </label>
          <label className="field">Seconds per trial
            <input type="number" min={1} max={60} value={d} onChange={(e) => { setSaved(false); setDuration(Math.min(60, Math.max(1, Number(e.target.value) || 1))); }} />
          </label>
          <button className="btn-primary" onClick={save}>Save</button>
          <span role="status" className="muted">{saved ? "Saved." : ""}</span>
        </div>
      </section>
      <section className="card">
        <h2>Profiler Service</h2>
        <p className="ink2">
          This UI calls the service through <code>/api</code>. Start the service with <code>python -m profiler_service</code>{" "}
          (default http://127.0.0.1:8010); point the UI elsewhere with <code>VAYUNX_API_URL</code> when starting Next.js.
        </p>
      </section>
    </div>
  );
}
