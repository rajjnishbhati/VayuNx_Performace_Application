/** OTLP/HTTP JSON encoding of the fast-path histograms. Pure: no Node built-ins (the Edge build uses it too). */

import type { LatencyHistogram } from "./histogram";

export const METRIC = "vayunx.crypto.duration";

export type Attrs = Record<string, string | number | boolean | undefined>;

function kv(attrs: Attrs) {
  return Object.entries(attrs)
    .filter(([, v]) => v !== undefined && v !== null)
    .map(([key, v]) => ({
      key,
      value: typeof v === "boolean" ? { boolValue: v } : typeof v === "number"
        ? (Number.isInteger(v) ? { intValue: String(v) } : { doubleValue: v }) : { stringValue: String(v) },
    }));
}

const nanos = (ms: number) => `${BigInt(Math.round(ms)) * 1000000n}`;

/** One explicit-bucket histogram data point per series, delta temporality, unit ns. */
export function buildRequest(resource: Attrs, startMs: number, endMs: number,
                             series: [Attrs, LatencyHistogram][]): string {
  const dataPoints = series.map(([attrs, h]) => {
    const { bounds, counts } = h.explicitBuckets();
    return {
      attributes: kv(attrs), startTimeUnixNano: nanos(startMs), timeUnixNano: nanos(endMs),
      count: String(h.count), sum: h.sum, min: h.min, max: h.max,
      explicitBounds: bounds, bucketCounts: counts.map(String),
    };
  });
  return JSON.stringify({
    resourceMetrics: [{
      resource: { attributes: kv(resource) },
      scopeMetrics: [{
        scope: { name: "vayunx-node" },
        metrics: [{ name: METRIC, unit: "ns", histogram: { aggregationTemporality: 1, dataPoints } }],
      }],
    }],
  });
}
