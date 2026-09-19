"""Log-bucketed latency histogram in integer nanoseconds (scheme "log2x8-ns").

Values 0-7 ns get exact buckets. Above that, every power of two is split into 8 equal
sub-buckets, so a bucket is at most 12.5% wide and a percentile read from the bucket midpoint
is within about 6.25% of the true value. count, sum, min and max are exact.

The scheme is documented so a non-Python SDK can produce identical buckets:
    index(ns) = ns                               if ns < 8
              = 8 + 8*(b - 4) + ((ns >> (b - 4)) & 7)   otherwise, where b = bit_length(ns)
"""

from __future__ import annotations

import math

SCHEME = "log2x8-ns"


def bucket_index(ns: int) -> int:
    if ns < 8:
        return max(0, ns)
    b = ns.bit_length()
    return 8 + 8 * (b - 4) + ((ns >> (b - 4)) & 7)


def bucket_bounds(index: int) -> tuple[int, int]:
    """[lo, hi) in ns for a bucket index."""
    if index < 8:
        return index, index + 1
    k, sub = divmod(index - 8, 8)
    return (8 + sub) << k, (9 + sub) << k


class LatencyHistogram:
    __slots__ = ("count", "sum_ns", "min_ns", "max_ns", "buckets")

    def __init__(self):
        self.count = 0
        self.sum_ns = 0
        self.min_ns: int | None = None
        self.max_ns: int | None = None
        self.buckets: dict[int, int] = {}

    def record(self, ns: int) -> None:
        self.count += 1
        self.sum_ns += ns
        if self.min_ns is None or ns < self.min_ns:
            self.min_ns = ns
        if self.max_ns is None or ns > self.max_ns:
            self.max_ns = ns
        idx = bucket_index(ns)
        self.buckets[idx] = self.buckets.get(idx, 0) + 1

    def merge(self, other: "LatencyHistogram") -> None:
        if not other.count:
            return
        self.count += other.count
        self.sum_ns += other.sum_ns
        self.min_ns = other.min_ns if self.min_ns is None else min(self.min_ns, other.min_ns)
        self.max_ns = other.max_ns if self.max_ns is None else max(self.max_ns, other.max_ns)
        for idx, c in other.buckets.items():
            self.buckets[idx] = self.buckets.get(idx, 0) + c

    def percentile(self, q: float) -> int | None:
        """Approximate q-th percentile (0-100): the midpoint of the bucket holding that rank, clamped to [min, max]."""
        if not self.count:
            return None
        rank = max(1, math.ceil(q / 100.0 * self.count))
        seen = 0
        for idx in sorted(self.buckets):
            seen += self.buckets[idx]
            if seen >= rank:
                lo, hi = bucket_bounds(idx)
                mid = (lo + hi - 1) // 2
                return min(max(mid, self.min_ns), self.max_ns)
        return self.max_ns

    def to_dict(self) -> dict:
        return {"scheme": SCHEME, "count": self.count, "sum_ns": self.sum_ns, "min_ns": self.min_ns,
                "max_ns": self.max_ns, "buckets": {str(i): self.buckets[i] for i in sorted(self.buckets)}}

    @classmethod
    def from_dict(cls, d: dict) -> "LatencyHistogram":
        if d.get("scheme", SCHEME) != SCHEME:
            raise ValueError(f"unsupported histogram scheme {d.get('scheme')!r}")
        h = cls()
        h.count, h.sum_ns, h.min_ns, h.max_ns = d["count"], d["sum_ns"], d["min_ns"], d["max_ns"]
        h.buckets = {int(k): int(v) for k, v in d["buckets"].items()}
        return h
