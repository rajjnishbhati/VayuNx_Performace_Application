/** SDK state, the per-call sink hooks report to, and the public init/shutdown/span/measure/status API. */

import { context, SpanStatusCode, trace, type Context, type Span } from "@opentelemetry/api";

import * as config from "./config";
import { gauges, resourceAttrs, tracing, type Tracing } from "./otel";
import { OpHistograms, Shipper, type Attrs } from "./ship";

export const VERSION = "0.3.0";
const ACTIVE_WINDOW_MS = 50; // a crypto call finished within the last 50 ms counts as crypto activity

export interface CallInfo {
  operation: string; // hash / verify / kdf / mac / sign / encrypt / decrypt / digest
  algorithm: string;
  params?: string;
  library: string;
  sync: boolean;
  inputBytes?: number;
  threadpool?: boolean; // async work that runs on the libuv threadpool
  /** series id cache (valid for idOwner only), so cached CallInfo objects skip the key lookup */
  id?: number;
  idOwner?: object;
}

export class State {
  readonly ophists = new OpHistograms();
  readonly counters: Record<string, number> = { calls: 0, spans: 0, errors: 0, internal_errors: 0 };
  readonly findings = { blocking_event_loop: 0, threadpool_queue: 0 };
  readonly hookStatus: Record<string, string> = {};
  readonly shipper: Shipper;
  readonly tracing: Tracing;
  readonly meters?: ReturnType<typeof gauges>;
  readonly threadpoolSize = Number(process.env.UV_THREADPOOL_SIZE) || 4;
  inflightThreadpool = 0;
  lastOpAt = 0;
  depth = 0; // > 0 while a synchronous hooked call runs: nested hooked calls belong to it
  private keyIds = new Map<string, number>();

  constructor(readonly cfg: config.Config) {
    const attrs = resourceAttrs(cfg, VERSION);
    this.tracing = tracing(cfg, attrs, VERSION);
    this.shipper = new Shipper(`${cfg.endpoint}/v1/metrics`, attrs, this.ophists, this.counters, cfg.exportIntervalMs,
      cfg.flushTimeoutMs, cfg.maxPendingExports);
    if (cfg.gauges) this.meters = gauges(cfg, attrs, () => this.active());
  }

  active(): boolean {
    return this.inflightThreadpool > 0 || performance.now() - this.lastOpAt < ACTIVE_WINDOW_MS;
  }

  /** Series id for (operation, algorithm, params, library, sync); cached per distinct combination. */
  seriesId(c: CallInfo): number {
    if (c.idOwner === this) return c.id!;
    const k = `${c.operation}|${c.algorithm}|${c.params ?? ""}|${c.library}|${c.sync ? 1 : 0}`;
    let id = this.keyIds.get(k);
    if (id === undefined) {
      const attrs: Attrs = { "crypto.operation": c.operation, "crypto.algorithm": c.algorithm, "crypto.params": c.params,
        "crypto.library": c.library, "crypto.sync": c.sync };
      id = this.ophists.keyId(attrs);
      this.keyIds.set(k, id);
    }
    c.id = id;
    c.idOwner = this;
    return id;
  }

  /** Fast path: a finished call. `ms` is wall time from call to result (queue wait included for async). */
  record(c: CallInfo, t0: number, t1: number, parent: Context | undefined, err?: unknown, cpuMs?: number, queued?: boolean): void {
    try {
      const ms = t1 - t0;
      this.lastOpAt = t1;
      if (err !== undefined) {
        this.counters.errors++;
      } else {
        this.counters.calls++;
        this.ophists.record(this.seriesId(c), Math.round(ms * 1e6));
      }
      if (ms >= this.cfg.slowMs || (err !== undefined && ms >= 0.1)) this.span(c, t0, ms, parent, err, cpuMs, queued);
    } catch {
      this.counters.internal_errors++;
    }
  }

  private span(c: CallInfo, t0: number, ms: number, parent: Context | undefined, err?: unknown, cpuMs?: number, queued?: boolean): void {
    const attrs: Attrs = { "crypto.operation": c.operation, "crypto.algorithm": c.algorithm, "crypto.library": c.library,
      "crypto.sync": c.sync };
    if (c.params) attrs["crypto.params"] = c.params;
    if (c.inputBytes !== undefined) attrs["crypto.input_bytes"] = c.inputBytes;
    if (cpuMs !== undefined) attrs["vayunx.cpu_time_ms"] = cpuMs;
    if (c.sync && ms >= this.cfg.slowMs) {
      // every synchronous call runs on this thread's event loop: nothing else is served meanwhile
      this.findings.blocking_event_loop++;
      attrs["vayunx.finding"] = "blocking_event_loop";
    } else if (queued) {
      this.findings.threadpool_queue++;
      attrs["vayunx.finding"] = "threadpool_queue";
    }
    const startWall = performance.timeOrigin + t0;
    const span = this.tracing.tracer.startSpan(`${c.operation} ${c.algorithm}`,
      { startTime: startWall, attributes: attrs as Record<string, string | number | boolean> }, parent ?? context.active());
    if (err !== undefined) span.setStatus({ code: SpanStatusCode.ERROR, message: errorName(err) }); // the type only
    span.end(startWall + ms);
    this.counters.spans++;
  }
}

function errorName(err: unknown): string {
  return err instanceof Error ? err.name : typeof err;
}

let state: State | undefined;
let lastStatus: Record<string, unknown> | undefined;
let exitHookInstalled = false;
const installers: ((st: State) => void)[] = [];
const uninstallers: (() => void)[] = [];

export function current(): State | undefined {
  return state;
}

/** Hook modules register here; they run on init() and are undone on shutdown(). */
export function registerHooks(install: (st: State) => void, uninstall: () => void): void {
  installers.push(install);
  uninstallers.push(uninstall);
}

export function init(opts: config.InitOptions = {}): Record<string, unknown> {
  if (state) return status();
  if (config.disabled()) return { initialized: false, reason: "VAYUNX_DISABLE is set" };
  try {
    const cfg = config.load(opts);
    const st = new State(cfg);
    st.shipper.start();
    state = st;
    if (cfg.hooks) for (const install of installers) install(st);
    if (!exitHookInstalled) {
      exitHookInstalled = true;
      // 'beforeExit' fires when the loop runs dry on a normal exit; async work is allowed there.
      process.once("beforeExit", () => { void shutdown(); });
    }
    return status();
  } catch (e) {
    state = undefined;
    return { initialized: false, reason: `${errorName(e)}: ${(e as Error)?.message ?? e}` };
  }
}

/** Restore every patched function, then flush with a hard deadline. Resolves true when everything was sent. */
export async function shutdown(timeoutMs = 2000): Promise<boolean> {
  const st = state;
  if (!st) return true;
  lastStatus = status();
  state = undefined; // hooks become pass-throughs from here on
  for (const undo of uninstallers) {
    try { undo(); } catch { /* keep going */ }
  }
  const deadline = new Promise<false>((r) => setTimeout(() => r(false), timeoutMs).unref());
  const work = (async () => {
    const [shipped, spans] = await Promise.all([
      st.shipper.stop(timeoutMs),
      st.tracing.provider.forceFlush().then(() => true, () => false),
      st.meters ? st.meters.provider.forceFlush().catch(() => undefined) : undefined,
    ]);
    st.meters?.close();
    await Promise.allSettled([st.tracing.provider.shutdown(), st.meters?.provider.shutdown()]);
    if (st.tracing.registeredGlobal) trace.disable();
    return Boolean(shipped && spans);
  })().catch(() => false);
  return Promise.race([work, deadline]);
}

export function status(): Record<string, unknown> {
  const st = state;
  if (!st) return { initialized: false, ...(lastStatus ? { lastRun: lastStatus } : {}) };
  const c = st.cfg;
  return {
    initialized: true, service: c.service, variant: c.variant, runId: c.runId, endpoint: c.endpoint, phase: c.phase,
    slowMs: c.slowMs, hooks: { ...st.hookStatus }, findings: { ...st.findings }, counters: { ...st.counters },
    threadpoolSize: st.threadpoolSize, registeredGlobalTracer: st.tracing.registeredGlobal,
  };
}

/** Run `fn` inside a named span; crypto spans inside become its children. Works for sync and async fn. */
export function span<T>(name: string, fn: (span?: Span) => T, attributes?: Attrs): T {
  const st = state;
  if (!st) return fn(undefined);
  let sp: Span;
  try {
    sp = st.tracing.tracer.startSpan(name, { attributes: attributes as Record<string, string | number | boolean> });
  } catch {
    return fn(undefined);
  }
  const cpu0 = process.cpuUsage();
  const end = (err?: unknown) => {
    try {
      const d = process.cpuUsage(cpu0);
      sp.setAttribute("vayunx.process_cpu_ms", (d.user + d.system) / 1000); // whole process, all threads
      if (err !== undefined) sp.setStatus({ code: SpanStatusCode.ERROR, message: errorName(err) });
      sp.end();
    } catch { /* never throw into the host */ }
  };
  let result: T;
  try {
    result = context.with(trace.setSpan(context.active(), sp), () => fn(sp));
  } catch (e) {
    end(e);
    throw e;
  }
  if (result && typeof (result as { then?: unknown }).then === "function") {
    return (result as unknown as Promise<unknown>).then((v) => { end(); return v; }, (e) => { end(e); throw e; }) as T;
  }
  end();
  return result;
}

/** Wrap a function so every call runs inside span(name). */
export function measure<A extends unknown[], R>(name: string, fn: (...args: A) => R): (...args: A) => R {
  return function (this: unknown, ...args: A): R {
    return span(name, () => fn.apply(this, args));
  };
}
