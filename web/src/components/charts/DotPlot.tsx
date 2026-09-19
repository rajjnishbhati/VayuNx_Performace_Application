"use client";

// App view: time per call, one row per algorithm, on a log scale (values span nanoseconds to seconds).
// Marks per row: faint trial medians, p50 -> p95 whisker, hollow p99 marker, median dot (ring) + direct label.

import { useState } from "react";
import { fmtNs } from "@/lib/format";
import type { Variant } from "@/lib/types";
import { useWidth } from "@/lib/useWidth";

const ROW_H = 52;
const M = { top: 10, right: 84, bottom: 40, left: 250 };
const LABEL_CHARS = 30;

/** Cut in the middle: the end often carries what tells variants apart ("· Node.js", a cost). */
function shorten(label: string): string {
  if (label.length <= LABEL_CHARS) return label;
  const tail = Math.floor((LABEL_CHARS - 1) / 2);
  return `${label.slice(0, LABEL_CHARS - 1 - tail)}…${label.slice(label.length - tail)}`;
}

/** Decade ticks read as whole numbers: "1 µs", "10 µs", "100 µs" (not "10.0 µs"). */
function tickLabel(ns: number): string {
  return fmtNs(ns).replace(/\.0+ /, " ");
}

export default function DotPlot({ variants, colors }: { variants: Variant[]; colors: Record<string, string> }) {
  const [ref, width] = useWidth<HTMLDivElement>();
  const [hover, setHover] = useState<{ key: string; x: number; y: number } | null>(null);

  const values = variants.flatMap((v) => [
    ...v.time_per_call.trial_medians_ns, v.time_per_call.p50_ns ?? NaN, v.time_per_call.p99_ns ?? NaN,
  ]).filter((x) => Number.isFinite(x) && x > 0);
  if (!values.length) return <p className="muted">No per-call timings to plot.</p>;
  const lo = 10 ** Math.floor(Math.log10(Math.min(...values) / 1.2));
  const hi = 10 ** Math.ceil(Math.log10(Math.max(...values) * 1.2));
  const plotW = Math.max(120, width - M.left - M.right);
  const x = (ns: number) => M.left + ((Math.log10(ns) - Math.log10(lo)) / (Math.log10(hi) - Math.log10(lo))) * plotW;
  const height = M.top + variants.length * ROW_H + M.bottom;
  const ticks: number[] = [];
  for (let k = Math.round(Math.log10(lo)); k <= Math.round(Math.log10(hi)); k++) ticks.push(10 ** k);

  const hovered = variants.find((v) => v.key === hover?.key);

  return (
    <div ref={ref} style={{ position: "relative" }}>
      <svg width={width} height={height} role="img"
           aria-label={`Time per call on a log scale. ${variants.map((v) => `${v.label}: median ${fmtNs(v.time_per_call.median_ns)}`).join("; ")}.`}>
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={M.top} y2={height - M.bottom} stroke="var(--grid)" strokeWidth={1} />
            <text x={x(t)} y={height - M.bottom + 18} textAnchor="middle" fontSize={12} fill="var(--muted)" className="num">
              {tickLabel(t)}
            </text>
          </g>
        ))}
        <text x={M.left + plotW / 2} y={height - 6} textAnchor="middle" fontSize={12} fill="var(--muted)">
          Time per call (log scale)
        </text>
        {variants.map((v, i) => {
          const cy = M.top + i * ROW_H + ROW_H / 2;
          const c = colors[v.key];
          const t = v.time_per_call;
          if (t.median_ns == null) return null;
          const xMed = x(t.median_ns);
          const xEnd = x(Math.max(t.p99_ns ?? t.median_ns, t.p95_ns ?? t.median_ns));
          return (
            <g key={v.key}
               tabIndex={0}
               role="listitem"
               aria-label={`${v.label}${v.is_reference ? " (reference)" : ""}: median ${fmtNs(t.median_ns)}, p95 ${fmtNs(t.p95_ns)}, p99 ${fmtNs(t.p99_ns)}, ${v.trials} trials`}
               onMouseMove={(e) => {
                 const box = (e.currentTarget.ownerSVGElement as SVGSVGElement).getBoundingClientRect();
                 setHover({ key: v.key, x: e.clientX - box.left, y: cy });
               }}
               onMouseLeave={() => setHover(null)}
               onFocus={() => setHover({ key: v.key, x: xMed, y: cy })}
               onBlur={() => setHover(null)}
               style={{ outline: "none" }}>
              {/* hit area: the whole row, far bigger than the marks */}
              <rect x={0} y={cy - ROW_H / 2} width={width} height={ROW_H}
                    fill={hover?.key === v.key ? "var(--surface-2)" : "transparent"} rx={6} />
              <circle cx={14} cy={cy} r={6} fill={c} />
              <text x={28} y={cy - 2} fontSize={14} fill="var(--ink)">
                {shorten(v.label)}
                {v.label.length > LABEL_CHARS && <title>{v.label}</title>}
              </text>
              <text x={28} y={cy + 14} fontSize={12} fill="var(--muted)">{v.is_reference ? "reference" : `${v.trials} trials`}</text>
              {t.trial_medians_ns.map((m, j) => (
                <circle key={j} cx={x(m)} cy={cy} r={3.5} fill={c} opacity={0.45} />
              ))}
              {t.p95_ns != null && (
                <line x1={xMed} x2={x(t.p95_ns)} y1={cy} y2={cy} stroke={c} strokeWidth={2} strokeLinecap="round" />
              )}
              {t.p95_ns != null && <line x1={x(t.p95_ns)} x2={x(t.p95_ns)} y1={cy - 5} y2={cy + 5} stroke={c} strokeWidth={2} strokeLinecap="round" />}
              {t.p99_ns != null && <circle cx={x(t.p99_ns)} cy={cy} r={4} fill="var(--surface)" stroke={c} strokeWidth={1.5} />}
              <circle cx={xMed} cy={cy} r={6} fill={c} stroke="var(--surface)" strokeWidth={2} />
              <text x={xEnd + 10} y={cy + 4} fontSize={13} fill="var(--ink-2)" className="num">{fmtNs(t.median_ns)}</text>
            </g>
          );
        })}
      </svg>
      <p className="muted" style={{ fontSize: 12, margin: "4px 0 0" }}>
        Dot = median of the trial medians · line = up to p95 · ring = p99 · faint dots = each trial&apos;s median.
      </p>
      {hovered && hover && (
        <div className="tooltip" role="status" style={{ left: Math.min(hover.x + 12, width - 220), top: hover.y + 14 }}>
          <div className="muted" style={{ fontSize: 12 }}>
            <span className="linekey" style={{ background: colors[hovered.key] }} />{hovered.label}
          </div>
          <div><span className="v">{fmtNs(hovered.time_per_call.median_ns)}</span> <span className="muted">median per call</span></div>
          <div><span className="v">{fmtNs(hovered.time_per_call.p95_ns)}</span> <span className="muted">p95</span> · <span className="v">{fmtNs(hovered.time_per_call.p99_ns)}</span> <span className="muted">p99</span></div>
          {hovered.time_per_call.range_ns && (
            <div className="muted">Trials {hovered.trials}: {fmtNs(hovered.time_per_call.range_ns[0])} – {fmtNs(hovered.time_per_call.range_ns[1])}</div>
          )}
          {hovered.flags.filter((f) => f.affects.includes("time_per_call")).map((f) => (
            <div key={f.code} style={{ color: "var(--warning-ink)", fontSize: 12 }}>⚠ {f.message}</div>
          ))}
        </div>
      )}
    </div>
  );
}
