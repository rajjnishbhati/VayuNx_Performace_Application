/** OpenTelemetry providers (traces + process/runtime gauges) exported over OTLP/HTTP protobuf. */

import * as os from "node:os";
import { randomFill } from "node:crypto";
import { monitorEventLoopDelay, performance, type EventLoopUtilization, type IntervalHistogram } from "node:perf_hooks";

import { context, trace, type Tracer } from "@opentelemetry/api";
import { AsyncLocalStorageContextManager } from "@opentelemetry/context-async-hooks";
import { OTLPMetricExporter } from "@opentelemetry/exporter-metrics-otlp-proto";
import { OTLPTraceExporter } from "@opentelemetry/exporter-trace-otlp-proto";
import { resourceFromAttributes } from "@opentelemetry/resources";
import { MeterProvider, PeriodicExportingMetricReader } from "@opentelemetry/sdk-metrics";
import { BasicTracerProvider, BatchSpanProcessor } from "@opentelemetry/sdk-trace-base";

import { authHeaders, type Config } from "./config";
import type { Attrs } from "./ship";

export function resourceAttrs(cfg: Config, version: string): Attrs {
  const attrs: Attrs = {
    "service.name": cfg.service, "vayunx.run_id": cfg.runId, "vayunx.variant": cfg.variant, "vayunx.phase": cfg.phase,
    "vayunx.sdk": `vayunx-node/${version}`, "telemetry.sdk.language": "nodejs", "process.runtime.name": "nodejs",
    "process.runtime.version": process.versions.node, "host.name": os.hostname(), "os.type": process.platform,
    "vayunx.runtime": process.env.NEXT_RUNTIME ? `next-${process.env.NEXT_RUNTIME}` : "node",
  };
  for (const k of Object.keys(attrs)) if (attrs[k] === undefined) delete attrs[k];
  return attrs;
}

export interface Tracing {
  provider: BasicTracerProvider;
  tracer: Tracer;
  registeredGlobal: boolean;
}

let contextManagerSet = false;

export function tracing(cfg: Config, attrs: Attrs, version: string): Tracing {
  // Offline mode stays quiet (spec A) because OTel diagnostics are a no-op unless the app set a diag logger;
  // we do not set one, so an app's own logger is never replaced.
  const exporter = new OTLPTraceExporter({ url: `${cfg.endpoint}/v1/traces`, timeoutMillis: cfg.flushTimeoutMs, headers: authHeaders(cfg) });
  const provider = new BasicTracerProvider({
    resource: resourceFromAttributes(attrs as Record<string, string | number | boolean>),
    spanProcessors: [new BatchSpanProcessor(exporter, {
      maxQueueSize: 2048, maxExportBatchSize: 512, scheduledDelayMillis: 1000, exportTimeoutMillis: cfg.flushTimeoutMs,
    })],
  });
  if (!contextManagerSet) {
    // true when no other context manager was registered first (then theirs keeps working for us too)
    contextManagerSet = context.setGlobalContextManager(new AsyncLocalStorageContextManager().enable()) || true;
  }
  const registeredGlobal = cfg.registerGlobal ? trace.setGlobalTracerProvider(provider) : false;
  return { provider, tracer: provider.getTracer("vayunx-node", version), registeredGlobal };
}

/** Reads process/machine/runtime state once per collection and serves every gauge from that snapshot. */
class GaugeReader {
  private lastCpu = process.cpuUsage();
  private lastWall = performance.now();
  private lastMachine = machineTimes();
  private elu: EventLoopUtilization = performance.eventLoopUtilization();
  private eld: IntervalHistogram = monitorEventLoopDelay({ resolution: 10 });
  private snap: Record<string, number> = {};
  private at = 0;
  private probeMs = 0;
  private probeTimer: NodeJS.Timeout;

  constructor(private active: () => boolean, intervalMs: number) {
    this.eld.enable();
    // Threadpool wait probe: a 1-byte randomFill runs on the libuv threadpool, so its latency is the
    // queue wait plus a negligible amount of work. Taken every interval; the gauge reports the latest.
    this.probeTimer = setInterval(() => {
      const t0 = performance.now();
      randomFill(Buffer.alloc(1), () => { this.probeMs = performance.now() - t0; });
    }, Math.max(250, intervalMs));
    this.probeTimer.unref();
  }

  close(): void {
    clearInterval(this.probeTimer);
    this.eld.disable();
  }

  snapshot(): Record<string, number> {
    const now = performance.now();
    if (now - this.at < 250) return this.snap;
    const cpu = process.cpuUsage();
    const wallUs = (now - this.lastWall) * 1000;
    const procCores = wallUs > 0 ? (cpu.user - this.lastCpu.user + cpu.system - this.lastCpu.system) / wallUs : 0;
    const m = machineTimes();
    const busy = m.busy - this.lastMachine.busy;
    const total = m.total - this.lastMachine.total;
    const machinePct = total > 0 ? (busy / total) * 100 : 0;
    const cores = os.cpus().length || 1;
    const elu = performance.eventLoopUtilization(this.elu);
    this.elu = performance.eventLoopUtilization();
    const p99 = this.eld.percentile(99) / 1e6;
    this.eld.reset();
    this.snap = {
      proc_cores_busy: procCores, proc_cpu_time_s: (cpu.user + cpu.system) / 1e6,
      proc_rss_mib: process.memoryUsage.rss() / 1048576, proc_peak_rss_mib: process.resourceUsage().maxRSS / 1024,
      machine_cpu_pct: machinePct, machine_other_cores_busy: Math.max(0, (machinePct / 100) * cores - procCores),
      machine_mem_available_mib: os.freemem() / 1048576, node_eventloop_delay_p99_ms: p99,
      node_eventloop_utilization: elu.utilization, node_threadpool_wait_ms: this.probeMs,
    };
    this.lastCpu = cpu;
    this.lastWall = now;
    this.lastMachine = m;
    this.at = now;
    return this.snap;
  }

  category(): string {
    return this.active() ? "cryptographic" : "general";
  }
}

function machineTimes(): { busy: number; total: number } {
  let busy = 0;
  let total = 0;
  for (const c of os.cpus()) {
    const t = c.times;
    const sum = t.user + t.nice + t.sys + t.idle + t.irq;
    total += sum;
    busy += sum - t.idle;
  }
  return { busy, total };
}

export const GAUGES: Record<string, [string, string]> = { // OTLP name -> [snapshot key, unit]; see profiler_service/otlp.py
  "vayunx.process.cores_busy": ["proc_cores_busy", "cores"],
  "vayunx.process.cpu_time": ["proc_cpu_time_s", "s"],
  "vayunx.process.rss_mib": ["proc_rss_mib", "MiB"],
  "vayunx.process.peak_rss_mib": ["proc_peak_rss_mib", "MiB"],
  "vayunx.machine.cpu_pct": ["machine_cpu_pct", "%"],
  "vayunx.machine.other_cores_busy": ["machine_other_cores_busy", "cores"],
  "vayunx.machine.mem_available_mib": ["machine_mem_available_mib", "MiB"],
  "vayunx.node.eventloop.delay_p99_ms": ["node_eventloop_delay_p99_ms", "ms"],
  "vayunx.node.eventloop.utilization": ["node_eventloop_utilization", "ratio"],
  "vayunx.node.threadpool.wait_ms": ["node_threadpool_wait_ms", "ms"],
};

export function gauges(cfg: Config, attrs: Attrs, active: () => boolean): { provider: MeterProvider; close(): void } {
  const reader = new GaugeReader(active, cfg.gaugeIntervalMs);
  const provider = new MeterProvider({
    resource: resourceFromAttributes(attrs as Record<string, string | number | boolean>),
    readers: [new PeriodicExportingMetricReader({
      exporter: new OTLPMetricExporter({ url: `${cfg.endpoint}/v1/metrics`, timeoutMillis: cfg.flushTimeoutMs, headers: authHeaders(cfg) }),
      exportIntervalMillis: cfg.gaugeIntervalMs, exportTimeoutMillis: Math.min(cfg.flushTimeoutMs, cfg.gaugeIntervalMs),
    })],
  });
  const meter = provider.getMeter("vayunx-node");
  for (const [name, [key, unit]] of Object.entries(GAUGES)) {
    meter.createObservableGauge(name, { unit }).addCallback((result) => {
      try {
        const v = reader.snapshot()[key];
        if (v !== undefined && Number.isFinite(v)) result.observe(v, { "vayunx.category": reader.category() });
      } catch { /* never throw into the host */ }
    });
  }
  return { provider, close: () => reader.close() };
}
