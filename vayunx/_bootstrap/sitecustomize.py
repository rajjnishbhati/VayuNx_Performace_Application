"""Imported by Python at startup when the launcher put this directory on PYTHONPATH (see vayunx/__main__.py).

Starts profiling before the app's own imports, then runs any other sitecustomize that ours shadowed.
Never raises: a profiler problem must not stop the app from starting.
"""

import os
import sys


def _start() -> None:
    if os.environ.get("VAYUNX_AUTO") != "1":
        return
    try:
        import vayunx

        st = vayunx.init()
        if not st.get("initialized"):
            print(f"vayunx: not profiling ({st.get('reason', 'unknown reason')})", file=sys.stderr)
    except Exception as exc:  # pragma: no cover - defensive
        print(f"vayunx: not profiling ({type(exc).__name__}: {exc})", file=sys.stderr)


def _chain() -> None:
    """Import the next sitecustomize on sys.path, if there is one."""
    here = os.path.dirname(os.path.abspath(__file__))
    ours = sys.modules.pop("sitecustomize", None)
    saved = sys.path[:]
    try:
        sys.path[:] = [p for p in sys.path if os.path.abspath(p or ".") != here]
        import importlib

        importlib.import_module("sitecustomize")
    except ImportError:
        if ours is not None:
            sys.modules["sitecustomize"] = ours
    except Exception as exc:  # the other sitecustomize failed: report it as Python would, keep going
        print(f"Error in sitecustomize; set PYTHONVERBOSE for traceback:\n{type(exc).__name__}: {exc}", file=sys.stderr)
    finally:
        sys.path[:] = saved


_start()
_chain()
