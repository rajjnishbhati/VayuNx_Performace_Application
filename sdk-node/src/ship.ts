/**
 * Ships fast-path latency histograms as OTLP/HTTP JSON (POST /v1/metrics), one explicit-bucket
 * histogram per (operation, algorithm, params, library, sync) series, delta temporality.
 *
 * Why not the OTel metrics SDK for this: recording through it costs far more than a hash call on the
 * fast path; we aggregate in-process and send once per interval, like the Python SDK.
 * Failed exports stay pending (bounded; oldest dropped and counted) and are retried on the next tick.
 * Timers are unref'd, so the profiler never keeps a process alive. Never throws into the host.
 */

import * as http from "node:http";
import * as https from "node:https";

import { LatencyHistogram } from "./histogram";
import { buildRequest, type Attrs } from "./otlpjson";

export { METRIC, buildRequest, type Attrs } from "./otlpjson";

export class OpHistograms {
  /** interned series id -> histogram; swapped out on drain */
  hists = new Map<number, LatencyHistogram>();
  private keyIds = new Map<string, number>();
  readonly keyAttrs: Attrs[] = [];
  private startMs = Date.now();

  keyId(attrs: Attrs): number {
    const k = JSON.stringify(attrs);
    let id = this.keyIds.get(k);
    if (id === undefined) {
      id = this.keyAttrs.length;
      this.keyIds.set(k, id);
      this.keyAttrs.push(attrs);
    }
    return id;
  }

  record(id: number, ns: number): void {
    let h = this.hists.get(id);
    if (h === undefined) {
      h = new LatencyHistogram();
      this.hists.set(id, h);
    }
    h.record(ns);
  }

  drain(): { startMs: number; endMs: number; series: [Attrs, LatencyHistogram][] } {
    const hists = this.hists;
    this.hists = new Map();
    const startMs = this.startMs;
    this.startMs = Date.now();
    return { startMs, endMs: this.startMs, series: [...hists].map(([id, h]) => [this.keyAttrs[id], h]) };
  }
}

export function post(url: string, body: string, timeoutMs: number): Promise<"ok" | "rejected" | "failed"> {
  return new Promise((resolve) => {
    let settled = false;
    const done = (r: "ok" | "rejected" | "failed") => { if (!settled) { settled = true; resolve(r); } };
    try {
      const u = new URL(url);
      const mod = u.protocol === "https:" ? https : http;
      const req = mod.request(u, {
        method: "POST", agent: false,
        headers: { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(body) },
      }, (res) => {
        res.resume();
        const code = res.statusCode ?? 0;
        // a permanent 4xx is dropped rather than retried forever
        done(code >= 200 && code < 300 ? "ok" : code >= 400 && code < 500 && code !== 408 && code !== 429 ? "rejected" : "failed");
      });
      req.setTimeout(timeoutMs, () => req.destroy(new Error("timeout")));
      req.on("error", () => done("failed"));
      req.on("socket", (s) => s.unref());
      req.end(body);
    } catch {
      done("failed");
    }
  });
}

export class Shipper {
  private pending: string[] = [];
  private timer?: NodeJS.Timeout;
  private busy?: Promise<boolean>;

  constructor(private url: string, private resource: Attrs, private ophists: OpHistograms,
              private counters: Record<string, number>, private intervalMs: number, private timeoutMs: number,
              private maxPending: number) {}

  start(): void {
    this.timer = setInterval(() => { void this.tick(Date.now() + this.timeoutMs); }, this.intervalMs);
    this.timer.unref();
  }

  tick(deadline: number): Promise<boolean> {
    if (this.busy) return this.busy.then(() => this.tick(deadline));
    this.busy = this.run(deadline).finally(() => { this.busy = undefined; });
    return this.busy;
  }

  private async run(deadline: number): Promise<boolean> {
    try {
      const { startMs, endMs, series } = this.ophists.drain();
      if (series.length) {
        this.pending.push(buildRequest(this.resource, startMs, endMs, series));
        while (this.pending.length > this.maxPending) {
          this.pending.shift();
          this.counters.dropped_exports = (this.counters.dropped_exports ?? 0) + 1;
        }
      }
      while (this.pending.length && Date.now() < deadline) {
        const r = await post(this.url, this.pending[0], Math.max(100, deadline - Date.now()));
        if (r === "failed") {
          this.counters.offline_attempts = (this.counters.offline_attempts ?? 0) + 1;
          return false;
        }
        if (r === "rejected") this.counters.rejected_exports = (this.counters.rejected_exports ?? 0) + 1;
        else this.counters.exports_sent = (this.counters.exports_sent ?? 0) + 1;
        this.pending.shift();
      }
      return this.pending.length === 0;
    } catch {
      this.counters.internal_errors = (this.counters.internal_errors ?? 0) + 1;
      return false;
    }
  }

  async stop(timeoutMs: number): Promise<boolean> {
    if (this.timer) clearInterval(this.timer);
    return this.tick(Date.now() + timeoutMs);
  }
}
