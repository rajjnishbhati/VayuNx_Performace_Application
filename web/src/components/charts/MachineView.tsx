"use client";

// Machine view: small multiples (one metric per chart), shared time axis, shaded regions where crypto ran.
// Lines are neutral ink (they are the machine/process, not an algorithm); algorithm identity lives in the
// shaded regions and the legend. Never a dual-axis chart.

import { useMemo, useState } from "react";
import { fmtBytes, fmtCores } from "@/lib/format";
import type { Timeseries } from "@/lib/types";
import { useWidth } from "@/lib/useWidth";

type MetricCfg = { key: string; title: string; fmt: (v: number) => string };
const METRICS: MetricCfg[] = [
  { key: "proc_cores_busy", title: "Cores busy — the benchmark process", fmt: (v) => `${fmtCores(v)} cores` },
  { key: "proc_rss_mib", title: "Memory (RSS) — the benchmark process", fmt: (v) => fmtBytes(v * 1024 * 1024) },
  { key: "machine_cpu_pct", title: "Whole-machine CPU", fmt: (v) => `${v.toFixed(0)}%` },
  { key: "machine_other_cores_busy", title: "Other work on the machine (noise check)", fmt: (v) => `${fmtCores(v)} cores` },
];
const M = { left: 64, right: 16, top: 22, bottom: 8 };
const PLOT_H = 96;
const AXIS_H = 30;
const GAP_S = 1.0; // no sample for longer than this = a gap between trials; the line breaks

function niceMax(v: number): number {
  if (v <= 0) return 1;
  const p = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10]) if (m * p >= v) return m * p;
  return 10 * p;
}

function nearest(points: [number, number, string | null][], t: number): [number, number, string | null] | null {
  let best: [number, number, string | null] | null = null;
  for (const p of points) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
  return best && Math.abs(best[0] - t) <= 0.5 ? best : null;
}

export default function MachineView({ ts, colors }: { ts: Timeseries; colors: Record<string, string> }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hoverT, setHoverT] = useState<number | null>(null);
  const metrics = METRICS.filter((m) => ts.metrics[m.key]?.points.length);
  const tMax = useMemo(() => Math.max(1, ...metrics.flatMap((m) => ts.metrics[m.key].points.map((p) => p[0])),
    ...ts.regions.map((r) => r.end_s)), [ts, metrics]);
  const plotW = Math.max(120, width - M.left - M.right);
  const x = (t: number) => M.left + (t / tMax) * plotW;
  const legend = Array.from(new Map(ts.regions.map((r) => [r.preset, r.variant])).entries());
  const region = hoverT == null ? undefined : ts.regions.find((r) => hoverT >= r.start_s && hoverT <= r.end_s);

  if (!metrics.length) return <p className="muted">No machine samples were recorded for this experiment.</p>;

  const onMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const box = e.currentTarget.getBoundingClientRect();
    const t = ((e.clientX - box.left - M.left) / plotW) * tMax;
    setHoverT(t < 0 || t > tMax ? null : t);
  };
  const onKey = (e: React.KeyboardEvent) => {
    const step = tMax / 100;
    if (e.key === "ArrowRight") setHoverT((h) => Math.min(tMax, (h ?? 0) + step));
    else if (e.key === "ArrowLeft") setHoverT((h) => Math.max(0, (h ?? tMax) - step));
    else if (e.key === "Escape") setHoverT(null);
    else return;
    e.preventDefault();
  };

  return (
    <div ref={ref} style={{ position: "relative" }} tabIndex={0} onKeyDown={onKey} onBlur={() => setHoverT(null)}
         aria-label="Machine metrics over time. Use the left and right arrow keys to move through time.">
      <div className="row" style={{ marginBottom: 8, fontSize: 13 }} aria-label="Legend">
        <span className="muted">Shaded while hashing:</span>
        {legend.map(([preset, label]) => (
          <span key={preset} className="row" style={{ gap: 6 }}>
            <span style={{ width: 14, height: 14, borderRadius: 3, background: colors[preset], opacity: 0.35 }} />
            {label}
          </span>
        ))}
      </div>
      {metrics.map((m, i) => {
        const pts = ts.metrics[m.key].points;
        const yMax = niceMax(Math.max(...pts.map((p) => p[1])));
        const last = i === metrics.length - 1;
        const h = M.top + PLOT_H + M.bottom + (last ? AXIS_H : 0);
        const y = (v: number) => M.top + PLOT_H - (v / yMax) * PLOT_H;
        let d = "";
        pts.forEach((p, j) => {
          const gap = j === 0 || p[0] - pts[j - 1][0] > GAP_S;
          d += `${gap ? "M" : "L"}${x(p[0]).toFixed(1)},${y(p[1]).toFixed(1)}`;
        });
        return (
          <svg key={m.key} width={width} height={h} onMouseMove={onMove} onMouseLeave={() => setHoverT(null)}
               role="img" aria-label={`${m.title}; peak ${m.fmt(Math.max(...pts.map((p) => p[1])))}`} style={{ display: "block" }}>
            <text x={M.left} y={14} fontSize={13} fill="var(--ink)" fontWeight={600}>{m.title}</text>
            {ts.regions.map((r, k) => (
              <rect key={k} x={x(r.start_s)} y={M.top} width={Math.max(1, x(r.end_s) - x(r.start_s))} height={PLOT_H}
                    fill={colors[r.preset]} opacity={0.12} />
            ))}
            {[0, yMax / 2, yMax].map((v) => (
              <g key={v}>
                <line x1={M.left} x2={M.left + plotW} y1={y(v)} y2={y(v)} stroke={v === 0 ? "var(--axis)" : "var(--grid)"} strokeWidth={1} />
                <text x={M.left - 8} y={y(v) + 4} textAnchor="end" fontSize={11} fill="var(--muted)" className="num">{m.fmt(v)}</text>
              </g>
            ))}
            <path d={d} fill="none" stroke="var(--ink-2)" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
            {hoverT != null && <line x1={x(hoverT)} x2={x(hoverT)} y1={M.top} y2={M.top + PLOT_H} stroke="var(--ink)" strokeWidth={1} />}
            {last && [0, 0.25, 0.5, 0.75, 1].map((f) => (
              <text key={f} x={x(f * tMax)} y={M.top + PLOT_H + 20} textAnchor={f === 0 ? "start" : f === 1 ? "end" : "middle"}
                    fontSize={11} fill="var(--muted)" className="num">{Math.round(f * tMax)} s</text>
            ))}
          </svg>
        );
      })}
      {hoverT != null && (
        <div className="tooltip" role="status" style={{ left: Math.min(x(hoverT) + 12, width - 230), top: 36 }}>
          <div className="muted" style={{ fontSize: 12 }}>
            {Math.round(hoverT * 10) / 10} s{region ? ` · ${region.variant}, trial ${region.trial}` : " · not measuring"}
          </div>
          {metrics.map((m) => {
            const p = nearest(ts.metrics[m.key].points, hoverT);
            return (
              <div key={m.key}><span className="v">{p ? m.fmt(p[1]) : "–"}</span> <span className="muted">{m.title.split(" — ")[0]}</span></div>
            );
          })}
        </div>
      )}
      <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>{ts.note} Machine lines are the whole machine or the benchmark process, not an algorithm.</p>
    </div>
  );
}

export function machineTable(ts: Timeseries) {
  const med = (xs: number[]) => (xs.length ? [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)] : null);
  return ts.regions.map((r) => {
    const within = (k: string) => (ts.metrics[k]?.points ?? []).filter((p) => p[0] >= r.start_s && p[0] <= r.end_s).map((p) => p[1]);
    const rss = within("proc_rss_mib");
    return {
      variant: r.variant, preset: r.preset, trial: r.trial, seconds: r.end_s - r.start_s, noisy: r.noisy,
      cores: med(within("proc_cores_busy")), peakRssMiB: rss.length ? Math.max(...rss) : null,
      machineCpu: med(within("machine_cpu_pct")), other: med(within("machine_other_cores_busy")),
    };
  });
}
