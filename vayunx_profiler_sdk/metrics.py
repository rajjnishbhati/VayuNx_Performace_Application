"""Process and machine metric readers (psutil), shared by the SDK sampler and the Lab runner.

Process ("cost") metrics - the measured process:
    proc_cores_busy     cores (CPU% of the process / 100; psutil CPU% is a share of ONE core)
    proc_cpu_time_s     s     cumulative user + system CPU time
    proc_rss_mib        MiB   resident set size
    proc_peak_rss_mib   MiB   peak working set (Windows only; psutil peak_wset)
    proc_threads        count
    proc_ctx_switches   count cumulative voluntary + involuntary
Machine ("noise check") metrics - the whole machine:
    machine_cpu_pct           %     all cores, averaged
    machine_cpu_pct_max_core  %     busiest single core
    machine_mem_available_mib MiB
    machine_swap_used_mib     MiB
    machine_load1             load  1-minute load average (emulated by psutil on Windows)
    machine_proc_count        count  (expensive: read only on "full" ticks)
    machine_cpu_freq_mhz      MHz    where available (full ticks only)
"""

from __future__ import annotations

import sys

import psutil

MIB = 1024 * 1024
UNITS = {
    "proc_cores_busy": "cores", "proc_cpu_time_s": "s", "proc_rss_mib": "MiB", "proc_peak_rss_mib": "MiB",
    "proc_threads": "count", "proc_ctx_switches": "count",
    "machine_cpu_pct": "%", "machine_cpu_pct_max_core": "%", "machine_mem_available_mib": "MiB",
    "machine_swap_used_mib": "MiB", "machine_load1": "load", "machine_proc_count": "count", "machine_cpu_freq_mhz": "MHz",
    "machine_other_cores_busy": "cores",
}


class ProcessReader:
    def __init__(self, pid: int | None = None):
        self.proc = psutil.Process(pid)

    def prime(self) -> None:
        self.proc.cpu_percent(None)  # first reading is meaningless; this starts the interval

    def read(self, full: bool = True) -> dict:
        """full=False skips thread count and context switches: on Windows each of those enumerates a
        system-wide snapshot (~1.5 ms measured on an i5-8400H) while the rest cost microseconds."""
        p = self.proc
        with p.oneshot():
            cpu = p.cpu_times()
            mem = p.memory_info()
            out = {"proc_cores_busy": p.cpu_percent(None) / 100.0, "proc_cpu_time_s": cpu.user + cpu.system,
                   "proc_rss_mib": mem.rss / MIB}
            if sys.platform == "win32":
                out["proc_peak_rss_mib"] = mem.peak_wset / MIB
            if full:
                ctx = p.num_ctx_switches()
                out["proc_threads"] = p.num_threads()
                out["proc_ctx_switches"] = ctx.voluntary + ctx.involuntary
        return out


class MachineReader:
    def __init__(self):
        self.logical_cpus = psutil.cpu_count(logical=True) or 1

    def prime(self) -> None:
        psutil.cpu_percent(None, percpu=True)
        try:
            psutil.getloadavg()  # on Windows this starts psutil's background load estimator
        except (AttributeError, OSError):
            pass

    def read(self, full: bool = False) -> dict:
        per_core = psutil.cpu_percent(None, percpu=True) or [0.0]
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        out = {"machine_cpu_pct": sum(per_core) / len(per_core), "machine_cpu_pct_max_core": max(per_core),
               "machine_mem_available_mib": vm.available / MIB, "machine_swap_used_mib": sw.used / MIB}
        try:
            out["machine_load1"] = psutil.getloadavg()[0]
        except (AttributeError, OSError):
            pass
        if full:
            out["machine_proc_count"] = len(psutil.pids())
            try:
                freq = psutil.cpu_freq()
                if freq and freq.current:
                    out["machine_cpu_freq_mhz"] = freq.current
            except (AttributeError, OSError, NotImplementedError):
                pass
        return out

    def other_cores_busy(self, machine: dict, proc: dict) -> float:
        """Cores busy with work other than the measured process (the noise signal)."""
        return max(0.0, machine["machine_cpu_pct"] / 100.0 * self.logical_cpus - proc["proc_cores_busy"])
