/**
 * Log-bucketed latency histogram in integer nanoseconds - scheme "log2x8-ns", identical to
 * vayunx_profiler_sdk/histogram.py (the tests check the two agree bucket for bucket):
 *
 *   index(ns) = ns                                  if ns < 8
 *             = 8 + 8*(b - 4) + ((ns >> (b - 4)) & 7)   otherwise, where b = bit_length(ns)
 *
 * Every power of two is split into 8 sub-buckets, so a bucket is at most 12.5 % wide; count, sum,
 * min and max are exact.
 */

export const SCHEME = "log2x8-ns";

export function bitLength(n: number): number {
  if (n < 4294967296) return 32 - Math.clz32(n);
  let b = 32;
  while (2 ** b <= n) b++;
  return b;
}

export function bucketIndex(ns: number): number {
  if (ns < 8) return ns > 0 ? Math.floor(ns) : 0;
  const b = bitLength(ns);
  return 8 + 8 * (b - 4) + (Math.floor(ns / 2 ** (b - 4)) & 7);
}

/** [lo, hi) in ns for a bucket index. */
export function bucketBounds(index: number): [number, number] {
  if (index < 8) return [index, index + 1];
  const k = Math.floor((index - 8) / 8);
  const sub = (index - 8) % 8;
  return [(8 + sub) * 2 ** k, (9 + sub) * 2 ** k];
}

export class LatencyHistogram {
  count = 0;
  sum = 0;
  min = Number.POSITIVE_INFINITY;
  max = 0;
  buckets = new Map<number, number>();

  record(ns: number): void {
    this.count++;
    this.sum += ns;
    if (ns < this.min) this.min = ns;
    if (ns > this.max) this.max = ns;
    const i = bucketIndex(ns);
    this.buckets.set(i, (this.buckets.get(i) ?? 0) + 1);
  }

  /** OTLP explicit bounds/counts equal to the buckets: [lo, hi) in integer ns == (lo-1, hi-1]. */
  explicitBuckets(): { bounds: number[]; counts: number[] } {
    const bounds: number[] = [];
    const counts: number[] = [];
    for (const idx of [...this.buckets.keys()].sort((a, b) => a - b)) {
      const [lo, hi] = bucketBounds(idx);
      if (bounds.length === 0 || bounds[bounds.length - 1] < lo - 1) {
        bounds.push(lo - 1);
        counts.push(0);
      }
      bounds.push(hi - 1);
      counts.push(this.buckets.get(idx)!);
    }
    counts.push(0);
    return { bounds, counts };
  }
}
