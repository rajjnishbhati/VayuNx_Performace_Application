/**
 * Edge light mode (Next.js middleware / Edge route handlers). No Node.js built-ins are used here.
 *
 *     // instrumentation.ts
 *     if (process.env.NEXT_RUNTIME === "edge") {
 *       const { initEdge } = await import("@vayunx/profiler/edge");
 *       initEdge({ service: "my-next-app" });
 *     }
 *
 * What it does: wraps Web Crypto (crypto.subtle digest/sign/verify/encrypt/decrypt/deriveBits/deriveKey),
 * aggregates latencies into the same log2x8-ns histograms, and sends them with fetch() - at most once per
 * export interval, piggy-backed on crypto calls, plus flushEdge() on demand. What it does not do: spans,
 * CPU time, memory or event-loop gauges (the Edge runtime exposes none of them), npm-package hooks.
 * An Edge isolate can be frozen or discarded at any time, so up to one interval of data can be lost.
 */

import { LatencyHistogram } from "./histogram";
import { buildRequest, type Attrs } from "./otlpjson";

export interface EdgeOptions {
  endpoint?: string;
  service?: string;
  variant?: string;
  runId?: string;
  exportIntervalMs?: number;
  apiToken?: string;
}

interface EdgeState {
  url: string;
  headers: Record<string, string>;
  resource: Attrs;
  intervalMs: number;
  hists: Map<string, [Attrs, LatencyHistogram]>;
  startMs: number;
  lastFlush: number;
  calls: number;
  errors: number;
  sendFailures: number;
  patched: [Record<string, unknown>, string, unknown][];
}

let st: EdgeState | undefined;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const env = (k: string): string | undefined => (globalThis as any).process?.env?.[k];

function algOf(alg: unknown, key?: unknown): { algorithm: string; params?: string } {
  const a = (typeof alg === "string" ? { name: alg } : alg ?? {}) as { name?: string; hash?: unknown; iterations?: number };
  const name = String(a.name ?? "?").toUpperCase();
  const hashOf = (h: unknown) => String(typeof h === "string" ? h : (h as { name?: string })?.name ?? "?").replace("SHA-", "SHA");
  if (name === "PBKDF2") return { algorithm: `PBKDF2-HMAC-${hashOf(a.hash)}`, params: a.iterations ? `i=${a.iterations}` : undefined };
  if (name === "HMAC") return { algorithm: `HMAC-${hashOf(a.hash ?? (key as { algorithm?: { hash?: unknown } })?.algorithm?.hash)}` };
  if (name.startsWith("SHA-")) return { algorithm: name.replace("SHA-", "SHA") };
  return { algorithm: String(a.name ?? "?") };
}

function record(op: string, alg: { algorithm: string; params?: string }, ms: number): void {
  const s = st;
  if (!s) return;
  const key = `${op}|${alg.algorithm}|${alg.params ?? ""}`;
  let entry = s.hists.get(key);
  if (!entry) {
    entry = [{ "crypto.operation": op, "crypto.algorithm": alg.algorithm, "crypto.params": alg.params,
      "crypto.library": "webcrypto (edge)", "crypto.sync": false }, new LatencyHistogram()];
    s.hists.set(key, entry);
  }
  entry[1].record(Math.round(ms * 1e6));
  s.calls++;
  if (Date.now() - s.lastFlush >= s.intervalMs) void flushEdge();
}

/** Send what has been aggregated so far. Resolves true when the service accepted it. Never rejects. */
export async function flushEdge(timeoutMs = 2000): Promise<boolean> {
  const s = st;
  if (!s || s.hists.size === 0) return true;
  const series = [...s.hists.values()];
  s.hists = new Map();
  const startMs = s.startMs;
  s.startMs = s.lastFlush = Date.now();
  try {
    const r = await fetch(s.url, {
      method: "POST", headers: { "Content-Type": "application/json", ...s.headers },
      body: buildRequest(s.resource, startMs, s.startMs, series), signal: AbortSignal.timeout(timeoutMs),
    });
    return r.ok;
  } catch {
    s.sendFailures++; // dropped: the Edge runtime gives us nowhere safe to buffer across isolates
    return false;
  }
}

export function initEdge(opts: EdgeOptions = {}): Record<string, unknown> {
  if (st) return edgeStatus();
  try {
    const subtle = (globalThis.crypto as { subtle?: object } | undefined)?.subtle;
    if (!subtle) return { initialized: false, reason: "no Web Crypto in this runtime" };
    const runId = opts.runId ?? env("VAYUNX_RUN_ID");
    const token = opts.apiToken ?? env("VAYUNX_API_TOKEN");
    st = {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      url: `${(opts.endpoint ?? env("VAYUNX_ENDPOINT") ?? "http://127.0.0.1:8010").replace(/\/+$/, "")}/v1/metrics`,
      resource: {
        "service.name": opts.service ?? env("VAYUNX_SERVICE") ?? "edge-app", "vayunx.variant": opts.variant ?? env("VAYUNX_VARIANT"),
        "vayunx.run_id": runId && /^[0-9a-f]{32}$/.test(runId) ? runId : crypto.randomUUID().replace(/-/g, ""),
        "vayunx.sdk": "vayunx-node-edge/0.3.0", "vayunx.runtime": "next-edge",
      },
      intervalMs: opts.exportIntervalMs ?? 5000, hists: new Map(), startMs: Date.now(), lastFlush: Date.now(),
      calls: 0, errors: 0, sendFailures: 0, patched: [],
    };
    const proto = Object.getPrototypeOf(subtle) as Record<string, unknown>;
    const methods: [string, string, number | undefined][] = [
      ["digest", "hash", undefined], ["sign", "sign", 1], ["verify", "verify", 1], ["encrypt", "encrypt", 1],
      ["decrypt", "decrypt", 1], ["deriveBits", "kdf", undefined], ["deriveKey", "kdf", undefined]];
    for (const [m, op, keyIdx] of methods) {
      const orig = proto[m];
      if (typeof orig !== "function") continue;
      proto[m] = function (this: unknown, ...args: unknown[]) {
        const t0 = performance.now();
        const p = (orig as (...a: unknown[]) => Promise<unknown>).apply(this, args);
        if (!st) return p;
        return p.then((v) => {
          try { record(op, algOf(args[0], keyIdx === undefined ? undefined : args[keyIdx]), performance.now() - t0); } catch { /* never throw */ }
          return v;
        }, (e) => { if (st) st.errors++; throw e; });
      };
      st.patched.push([proto, m, orig]);
    }
    return edgeStatus();
  } catch (e) {
    st = undefined;
    return { initialized: false, reason: e instanceof Error ? e.message : String(e) };
  }
}

export async function shutdownEdge(timeoutMs = 2000): Promise<boolean> {
  const s = st;
  if (!s) return true;
  const ok = await flushEdge(timeoutMs);
  for (const [owner, key, orig] of s.patched) owner[key] = orig;
  st = undefined;
  return ok;
}

export function edgeStatus(): Record<string, unknown> {
  const s = st;
  if (!s) return { initialized: false };
  return { initialized: true, mode: "edge-light", runId: s.resource["vayunx.run_id"], service: s.resource["service.name"],
    variant: s.resource["vayunx.variant"], calls: s.calls, errors: s.errors, sendFailures: s.sendFailures };
}
