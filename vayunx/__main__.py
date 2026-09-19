"""Launcher: profile any Python command without changing its code.

    python -m vayunx run -- python app.py            (or: vayunx-run python app.py)
    python -m vayunx run --variant argon2id -- uvicorn app:app

The child gets VAYUNX_AUTO=1 and our bootstrap directory on PYTHONPATH, so Python imports
vayunx/_bootstrap/sitecustomize.py at startup and hooks are installed before the app imports anything
(early-bound references like `from hashlib import md5` are then captured too). An existing
sitecustomize is still run after ours. Every Python child process started with the same environment
(workers, reloaders) is profiled as well and reports into the same run.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

import vayunx

BOOTSTRAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bootstrap")
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def child_env(args: argparse.Namespace, base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    parts = [BOOTSTRAP, PACKAGE_ROOT] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    env["VAYUNX_AUTO"] = "1"
    for opt, var in (("endpoint", "VAYUNX_ENDPOINT"), ("service", "VAYUNX_SERVICE"), ("variant", "VAYUNX_VARIANT"),
                     ("run_id", "VAYUNX_RUN_ID"), ("phase", "VAYUNX_PHASE"), ("slow_ms", "VAYUNX_SLOW_MS")):
        value = getattr(args, opt, None)
        if value is not None:
            env[var] = str(value)
    return env


def _parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog, description="Run a Python command with VAYUNX crypto profiling.")
    p.add_argument("--endpoint", help="profiler service URL (default $VAYUNX_ENDPOINT or http://127.0.0.1:8010)")
    p.add_argument("--service", help="service name shown in the UI (default $VAYUNX_SERVICE or the script name)")
    p.add_argument("--variant", help="variant label, e.g. md5 or argon2id (default $VAYUNX_VARIANT)")
    p.add_argument("--run-id", dest="run_id", help="32-char hex run id (default: new per process)")
    p.add_argument("--phase", choices=["baseline", "remediated"])
    p.add_argument("--slow-ms", dest="slow_ms", type=float, help="crypto calls at least this slow become spans (default 1)")
    p.add_argument("command", nargs=argparse.REMAINDER, help="the command to run, after --")
    return p


def run_main(argv: list[str] | None = None, prog: str = "vayunx-run") -> int:
    args = _parser(prog).parse_args(sys.argv[1:] if argv is None else argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        print(f"usage: {prog} [options] -- <command ...>", file=sys.stderr)
        return 2
    try:
        command = [shutil.which(command[0]) or command[0], *command[1:]]  # Windows: finds python -> python.exe
        return subprocess.call(command, env=child_env(args))
    except KeyboardInterrupt:  # the child got the same Ctrl+C; let it finish its own shutdown
        return 130
    except FileNotFoundError:
        print(f"{prog}: command not found: {command[0]}", file=sys.stderr)
        return 127


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["run"]:
        return run_main(argv[1:], prog="python -m vayunx run")
    if argv[:1] in (["--version"], ["version"]):
        print(f"vayunx {vayunx.__version__}")
        return 0
    print("usage: python -m vayunx run [options] -- <command ...>\n       python -m vayunx --version", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
