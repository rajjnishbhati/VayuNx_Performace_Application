"""Crypto Lab CLI.

    python -m vayunx_lab presets
    python -m vayunx_lab run --presets md5,argon2id-rfc9106-low [--trials 5] [--duration 10] [--reference md5]

Results go to the Profiler Service database (VAYUNX_PROFILER_DB_URL, default profiler.db), so the
compare screen can show them.
"""

from __future__ import annotations

import argparse
import sys

from vayunx_lab.presets import PRESETS
from vayunx_lab.security import security_note


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="vayunx_lab", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("presets", help="list built-in presets")
    r = sub.add_parser("run", help="run an experiment")
    r.add_argument("--presets", required=True, help="comma-separated preset ids")
    r.add_argument("--trials", type=int, default=5)
    r.add_argument("--duration", type=float, default=10.0, help="seconds per trial")
    r.add_argument("--concurrency", type=int, default=1)
    r.add_argument("--reference", default=None, help="preset id to compare against (default: the first)")
    r.add_argument("--db-url", default=None)
    args = p.parse_args(argv)

    if args.cmd == "presets":
        for preset in PRESETS.values():
            note = security_note(preset)
            badge = "safe for passwords" if note["safe_for_passwords"] else "NOT for passwords"
            print(f"{preset.id:<22} {preset.label:<28} [{badge}] {preset.library_version()}")
        return 0

    from profiler_service import config
    from profiler_service.db import make_engine, make_sessionmaker
    from profiler_service.lab_store import LabStore
    from vayunx_lab.runner import ExperimentRunner

    store = LabStore(make_sessionmaker(make_engine(args.db_url or config.DB_URL)))
    runner = ExperimentRunner(store, [s.strip() for s in args.presets.split(",") if s.strip()], trials=args.trials,
                              duration_s=args.duration, concurrency=args.concurrency, reference=args.reference,
                              log=lambda msg: print(msg, flush=True))
    exp_id = runner.create()
    print(f"experiment {exp_id}", flush=True)
    status = runner.run(exp_id)
    print(f"{status}: experiment {exp_id}")
    return 0 if status == "complete" else 1


if __name__ == "__main__":
    sys.exit(main())
