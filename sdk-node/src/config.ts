/** Settings from init() options, falling back to environment variables (same names as the Python SDK). */

import { randomUUID } from "node:crypto";
import * as path from "node:path";

export interface InitOptions {
  endpoint?: string;
  service?: string;
  variant?: string;
  runId?: string;
  phase?: "baseline" | "remediated";
  /** crypto calls at least this slow also become spans (default 1 ms) */
  slowMs?: number;
  exportIntervalMs?: number;
  gaugeIntervalMs?: number;
  flushTimeoutMs?: number;
  gauges?: boolean;
  hooks?: boolean;
  /** register our TracerProvider as the global one when none is set, so framework spans (Next.js) are exported too */
  registerGlobal?: boolean;
}

export interface Config {
  endpoint: string;
  service: string;
  variant?: string;
  runId: string;
  phase?: "baseline" | "remediated";
  slowMs: number;
  exportIntervalMs: number;
  gaugeIntervalMs: number;
  flushTimeoutMs: number;
  gauges: boolean;
  hooks: boolean;
  registerGlobal: boolean;
  maxPendingExports: number;
}

function num(name: string, fallback: number): number {
  const v = Number(process.env[name]);
  return Number.isFinite(v) && process.env[name] !== "" && process.env[name] !== undefined ? v : fallback;
}

const RUN_ID = /^[0-9a-f]{32}$/;

export function load(o: InitOptions = {}): Config {
  const env = process.env;
  const script = process.argv[1] ? path.basename(process.argv[1]).replace(/\.[cm]?[jt]s$/, "") : "";
  const phase = o.phase ?? (env.VAYUNX_PHASE as Config["phase"]);
  const runId = o.runId ?? env.VAYUNX_RUN_ID;
  return {
    endpoint: (o.endpoint ?? env.VAYUNX_ENDPOINT ?? "http://127.0.0.1:8010").replace(/\/+$/, ""),
    service: o.service ?? env.VAYUNX_SERVICE ?? env.OTEL_SERVICE_NAME ?? (script || "node-app"),
    variant: o.variant ?? env.VAYUNX_VARIANT ?? undefined,
    runId: runId && RUN_ID.test(runId) ? runId : randomUUID().replace(/-/g, ""),
    phase: phase === "baseline" || phase === "remediated" ? phase : undefined,
    slowMs: o.slowMs ?? num("VAYUNX_SLOW_MS", 1),
    exportIntervalMs: o.exportIntervalMs ?? num("VAYUNX_EXPORT_INTERVAL_MS", 5000),
    gaugeIntervalMs: o.gaugeIntervalMs ?? num("VAYUNX_GAUGE_INTERVAL_MS", 1000),
    flushTimeoutMs: o.flushTimeoutMs ?? num("VAYUNX_FLUSH_TIMEOUT_MS", 2000),
    gauges: o.gauges ?? env.VAYUNX_GAUGES !== "0",
    hooks: o.hooks ?? env.VAYUNX_HOOKS !== "0",
    registerGlobal: o.registerGlobal ?? true,
    maxPendingExports: 60,
  };
}

export function disabled(): boolean {
  return ["1", "true", "yes"].includes(process.env.VAYUNX_DISABLE ?? "");
}
