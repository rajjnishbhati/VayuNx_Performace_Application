"""Human-readable numbers for the compare output.

* Durations and sizes pick their unit automatically and show 3 significant digits
  (580 ns, 1.10 µs, 38.3 ms, 3.24 s; 512 B, 99.4 MiB).
* A change of 2x or more (either way) is a ratio ("about 35,000× slower", "2.0× faster");
  a smaller change is a percentage ("-38.3%"). Ratios of 100x or more are rounded to 2 significant
  digits and prefixed "about".
* CPU% from psutil is % of ONE logical core, so cores busy = CPU% / 100.
Direction words ("slower", "more") are neutral descriptions - for password hashing, slower is the point.
"""

from __future__ import annotations

import math

RATIO_THRESHOLD = 2.0


def _round3(v: float) -> float:
    return float(f"{v:.3g}")


def _sig3(v: float) -> str:
    r = _round3(v)
    if r == 0:
        return "0"
    decimals = max(0, 2 - math.floor(math.log10(abs(r))))
    return f"{r:.{decimals}f}"


def fmt_duration_ns(ns: float | None) -> str:
    if ns is None:
        return "-"
    for scale, unit in ((1, "ns"), (1e3, "µs"), (1e6, "ms")):
        if abs(_round3(ns / scale)) < 1000:
            return f"{_sig3(ns / scale)} {unit}"
    return f"{_sig3(ns / 1e9)} s"


def fmt_duration_ms(ms: float | None) -> str:
    return "-" if ms is None else fmt_duration_ns(ms * 1e6)


def fmt_bytes(n: float | None) -> str:
    if n is None:
        return "-"
    if abs(n) < 1024:
        return f"{n:.0f} B"
    for scale, unit in ((1024, "KiB"), (1024 ** 2, "MiB")):
        if abs(_round3(n / scale)) < 1024:
            return f"{_sig3(n / scale)} {unit}"
    return f"{_sig3(n / 1024 ** 3)} GiB"


def fmt_mib(mib: float | None) -> str:
    return "-" if mib is None else fmt_bytes(mib * 1024 * 1024)


def _fmt_ratio(r: float) -> str:
    if r < 10:
        return f"{r:.1f}×"
    if r < 100:
        return f"{r:.0f}×"
    rounded = round(r, -int(math.floor(math.log10(r))) + 1)  # 2 significant digits
    return f"about {rounded:,.0f}×"


def fmt_change(before: float | None, after: float | None, up: str = "slower", down: str = "faster") -> str:
    if before is None or after is None:
        return "n/a"
    if before == 0:
        return "n/a (baseline is 0)"
    r = after / before
    if r >= RATIO_THRESHOLD:
        return f"{_fmt_ratio(r)} {up}"
    if 0 < r <= 1 / RATIO_THRESHOLD:
        return f"{_fmt_ratio(1 / r)} {down}"
    return f"{(r - 1) * 100:+.1f}%"


def cores_busy(cpu_pct: float) -> float:
    return cpu_pct / 100.0


def fmt_cores_value(cpu_pct: float | None) -> str:
    if cpu_pct is None:
        return "-"
    v = cores_busy(cpu_pct)
    return f"{v:.2f}" if v < 1 else f"{v:.1f}"


def fmt_cores(cpu_pct: float | None) -> str:
    return f"{fmt_cores_value(cpu_pct)} cores busy"
