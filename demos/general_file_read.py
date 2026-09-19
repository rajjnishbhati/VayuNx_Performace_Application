"""General-category demo - no cryptography: timing a file read with two chunk sizes.

Proves the same span/sample mechanism works for ordinary code-level questions. Two runs are
recorded in this one process:
    baseline   "read-4kib-chunks"  - reads the file 4 KiB at a time
    remediated "read-1mib-chunks"  - reads the same file 1 MiB at a time
("baseline"/"remediated" are the Service's generic phase names; here they just mean before/after
a code change.)

The test file is random bytes in the OS temp directory, read once *before* profiling so both runs
see a warm OS page cache (otherwise the first run would pay for cold disk reads). Deleted at the end.

    python demos/general_file_read.py [--service-url http://127.0.0.1:8010] [--size-mib 32] [--reads 5]

Prints BASELINE_RUN_ID=<id> and REMEDIATED_RUN_ID=<id>.
"""

from __future__ import annotations

import argparse
import os
import tempfile

from vayunx_profiler_sdk import ProfilerClient

SERVICE_NAME = "file-io-demo"
VARIANTS = (("baseline", "read-4kib-chunks", 4 * 1024), ("remediated", "read-1mib-chunks", 1024 * 1024))


def read_file(run, path: str, chunk_size: int) -> int:
    with run.span("open_file", category="general"):
        f = open(path, "rb")
    try:
        with run.span("read_chunks", category="general", attributes={"chunk_size_bytes": chunk_size}):
            total = 0
            while chunk := f.read(chunk_size):
                total += len(chunk)
    finally:
        with run.span("close_file", category="general"):
            f.close()
    return total


def main(argv=None) -> tuple[str, str]:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--service-url", default="http://127.0.0.1:8010")
    p.add_argument("--size-mib", type=int, default=32)
    p.add_argument("--reads", type=int, default=5)
    p.add_argument("--interval-ms", type=int, default=100)
    args = p.parse_args(argv)

    fd, path = tempfile.mkstemp(prefix="vayunx-profiler-demo-", suffix=".bin")
    run_ids = {}
    try:
        with os.fdopen(fd, "wb") as f:
            for _ in range(args.size_mib):
                f.write(os.urandom(1024 * 1024))
        expected = args.size_mib * 1024 * 1024
        with open(path, "rb") as f:  # warm the OS page cache (not profiled)
            while f.read(1024 * 1024):
                pass

        profiler = ProfilerClient(service_url=args.service_url, service_name=SERVICE_NAME)
        for phase, label, chunk_size in VARIANTS:
            with profiler.run(label=label, phase=phase, metadata={"demo": "general_file_read.py", "file_size_mib": args.size_mib,
                                                                    "reads": args.reads, "chunk_size_bytes": chunk_size}) as run:
                profiler.start_sampling(interval_ms=args.interval_ms, metrics=["cpu_pct", "memory_mb"], category="general")
                for _ in range(args.reads):
                    with run.span("read_file", category="general", attributes={"chunk_size_bytes": chunk_size}):
                        if read_file(run, path, chunk_size) != expected:
                            raise SystemExit("short read")
                stats = profiler.stop_sampling()
            run_ids[phase] = run.run_id
            print(f"{phase}: spans_sent={run.spans_sent} samples_sent={stats['samples_sent']}")
    finally:
        os.remove(path)

    print(f"BASELINE_RUN_ID={run_ids['baseline']}")
    print(f"REMEDIATED_RUN_ID={run_ids['remediated']}")
    return run_ids["baseline"], run_ids["remediated"]


if __name__ == "__main__":
    main()
