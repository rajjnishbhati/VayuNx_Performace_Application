import pytest

from profiler_service.formatting import cores_busy, fmt_bytes, fmt_change, fmt_cores, fmt_duration_ms, fmt_duration_ns, fmt_mib


@pytest.mark.parametrize("ns, text", [
    (0, "0 ns"), (580, "580 ns"), (999, "999 ns"), (999.6, "1.00 µs"), (1_000, "1.00 µs"), (1_100, "1.10 µs"),
    (38_260_000, "38.3 ms"), (129_786_000, "130 ms"), (3_244_640_000, "3.24 s"), (125_000_000_000, "125 s"),
])
def test_duration_auto_units(ns, text):
    assert fmt_duration_ns(ns) == text


def test_duration_from_ms():
    assert fmt_duration_ms(0.0036) == "3.60 µs"
    assert fmt_duration_ms(129.786) == "130 ms"
    assert fmt_duration_ms(None) == "-"


@pytest.mark.parametrize("n, text", [
    (512, "512 B"), (1024, "1.00 KiB"), (99.4 * 1024 * 1024, "99.4 MiB"), (3 * 1024 ** 3, "3.00 GiB"),
])
def test_bytes_auto_units(n, text):
    assert fmt_bytes(n) == text


def test_mib_input():
    assert fmt_mib(99.46) == "99.5 MiB" and fmt_mib(0.5) == "512 KiB"


@pytest.mark.parametrize("before, after, text", [
    (1.1, 38_260, "about 35,000× slower"),
    (0.0036, 129.79, "about 36,000× slower"),
    (100, 250, "2.5× slower"),
    (100, 3_500, "35× slower"),
    (100, 110, "+10.0%"),
    (100, 95, "-5.0%"),
    (100, 50, "2.0× faster"),
    (176.6, 108.9, "-38.3%"),
    (0, 5, "n/a (baseline is 0)"),
    (None, 5, "n/a"),
])
def test_change_uses_ratio_for_large_and_percent_for_small(before, after, text):
    assert fmt_change(before, after) == text


def test_change_custom_direction_words():
    assert fmt_change(33.5, 99.5, up="more", down="less") == "3.0× more"
    assert fmt_change(99.5, 33.5, up="more", down="less") == "3.0× less"


def test_cores_busy():
    assert cores_busy(360.0) == pytest.approx(3.6)
    assert fmt_cores(360.0) == "3.6 cores busy"
    assert fmt_cores(95.0) == "0.95 cores busy"
    assert fmt_cores(0.0) == "0.00 cores busy"
