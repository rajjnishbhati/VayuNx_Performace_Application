"""A throwaway PostgreSQL server for tests: `VAYUNX_TEST_DB=postgres pytest`.

The server comes from, in order: VAYUNX_TEST_PG_URL (an existing server you own, e.g.
postgresql+psycopg://user:pw@localhost:5432/postgres - tests create and drop their own databases on it),
or VAYUNX_PG_BIN (a folder with initdb/pg_ctl, e.g. the portable EnterpriseDB zip), which is started in a
temporary directory on a free port and stopped at the end of the session.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import tempfile
import uuid
from pathlib import Path

from sqlalchemy import create_engine, text


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class PgServer:
    def __init__(self):
        self.admin_url = os.environ.get("VAYUNX_TEST_PG_URL")
        self._dir = None
        self._bin = None
        if self.admin_url:
            return
        bin_dir = os.environ.get("VAYUNX_PG_BIN")
        if not bin_dir or not (Path(bin_dir) / ("initdb.exe" if os.name == "nt" else "initdb")).exists():
            raise RuntimeError("set VAYUNX_TEST_PG_URL, or VAYUNX_PG_BIN to a folder containing initdb and pg_ctl")
        self._bin = Path(bin_dir)
        self._dir = Path(tempfile.mkdtemp(prefix="vayunx-pg-"))
        port = _free_port()
        data = self._dir / "data"
        subprocess.run([str(self._bin / "initdb"), "-D", str(data), "-U", "vayunx", "-A", "trust", "-E", "UTF8",
                        "--no-locale"], check=True, capture_output=True)
        # no pipes: the server inherits pg_ctl's handles, and a captured pipe would never reach EOF (it logs to pg.log)
        subprocess.run([str(self._bin / "pg_ctl"), "-D", str(data), "-l", str(self._dir / "pg.log"), "-w", "start",
                        "-o", f"-p {port} -h 127.0.0.1"], check=True, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        self.admin_url = f"postgresql+psycopg://vayunx@127.0.0.1:{port}/postgres"

    def new_database(self) -> str:
        name = f"t_{uuid.uuid4().hex[:12]}"
        engine = create_engine(self.admin_url, isolation_level="AUTOCOMMIT")
        with engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        engine.dispose()
        return self.admin_url.rsplit("/", 1)[0] + f"/{name}"

    def stop(self) -> None:
        if self._dir is None:
            return
        subprocess.run([str(self._bin / "pg_ctl"), "-D", str(self._dir / "data"), "-m", "immediate", "stop"],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        shutil.rmtree(self._dir, ignore_errors=True)
