import random

import pytest

from vayunx_profiler_sdk.histogram import SCHEME, LatencyHistogram, bucket_bounds, bucket_index


def test_bucket_bounds_contain_value_and_are_monotonic():
    last_idx = -1
    for ns in [0, 1, 2, 3, 7, 8, 9, 15, 16, 100, 999, 1_000, 1_234_567, 38_250_000, 10**11]:
        idx = bucket_index(ns)
        lo, hi = bucket_bounds(idx)
        assert lo <= ns < hi, (ns, idx, lo, hi)
        assert idx >= last_idx
        last_idx = idx


def test_bucket_relative_width_is_bounded():
    for ns in [1_000, 55_555, 38_250_000, 2_000_000_000]:
        lo, hi = bucket_bounds(bucket_index(ns))
        assert (hi - lo) / lo <= 0.125 + 1e-9  # 8 sub-buckets per power of two


def test_exact_count_sum_min_max():
    h = LatencyHistogram()
    for v in (1100, 580, 19000, 1100):
        h.record(v)
    assert (h.count, h.sum_ns, h.min_ns, h.max_ns) == (4, 21780, 580, 19000)


@pytest.mark.parametrize("q", [50, 95, 99])
def test_percentiles_within_bucket_error(q):
    rng = random.Random(7)
    values = [int(rng.lognormvariate(10, 0.8)) + 1 for _ in range(20_000)]
    h = LatencyHistogram()
    for v in values:
        h.record(v)
    exact = sorted(values)[min(len(values) - 1, int(round(q / 100 * len(values))) - 1)]
    assert abs(h.percentile(q) - exact) / exact <= 0.07


def test_percentile_clamped_to_observed_range():
    h = LatencyHistogram()
    h.record(1000)
    assert h.percentile(50) == 1000 and h.percentile(99) == 1000


def test_empty_histogram():
    h = LatencyHistogram()
    assert h.count == 0 and h.percentile(50) is None
    assert h.to_dict()["count"] == 0


def test_merge_and_roundtrip():
    a, b, both = LatencyHistogram(), LatencyHistogram(), LatencyHistogram()
    for v in range(1, 5000, 7):
        a.record(v)
        both.record(v)
    for v in range(10_000, 90_000, 13):
        b.record(v)
        both.record(v)
    a.merge(b)
    assert a.to_dict() == both.to_dict()
    restored = LatencyHistogram.from_dict(both.to_dict())
    assert restored.to_dict() == both.to_dict() and restored.percentile(95) == both.percentile(95)
    assert both.to_dict()["scheme"] == SCHEME
