// Mirrors profiler_service/formatting.py: automatic units at 3 significant digits.

function round3(v: number): number {
  return Number(v.toPrecision(3));
}

function sig3(v: number): string {
  const r = round3(v);
  if (r === 0) return "0";
  const decimals = Math.max(0, 2 - Math.floor(Math.log10(Math.abs(r))));
  return r.toFixed(decimals);
}

export function fmtNs(ns: number | null | undefined): string {
  if (ns == null || Number.isNaN(ns)) return "–";
  const units: [number, string][] = [[1, "ns"], [1e3, "µs"], [1e6, "ms"]];
  for (const [scale, unit] of units) if (Math.abs(round3(ns / scale)) < 1000) return `${sig3(ns / scale)} ${unit}`;
  return `${sig3(ns / 1e9)} s`;
}

export function fmtBytes(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "–";
  if (Math.abs(n) < 1024) return `${Math.round(n)} B`;
  const units: [number, string][] = [[1024, "KiB"], [1024 ** 2, "MiB"]];
  for (const [scale, unit] of units) if (Math.abs(round3(n / scale)) < 1024) return `${sig3(n / scale)} ${unit}`;
  return `${sig3(n / 1024 ** 3)} GiB`;
}

export function fmtCores(c: number | null | undefined): string {
  if (c == null || Number.isNaN(c)) return "–";
  return c < 1 ? c.toFixed(2) : c < 10 ? c.toFixed(1) : Math.round(c).toString();
}

export function fmtSeconds(s: number | null | undefined): string {
  if (s == null) return "–";
  if (s < 60) return `${Math.max(1, Math.round(s))} s`;
  const m = Math.floor(s / 60);
  return `${m} min ${Math.round(s - m * 60)} s`;
}

export function fmtInt(n: number | null | undefined): string {
  return n == null ? "–" : Math.round(n).toLocaleString("en-US");
}

export function fmtDate(iso: string | null | undefined): string {
  if (!iso) return "–";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}
