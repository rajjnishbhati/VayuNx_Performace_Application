"""Copy every row from one Profiler database to another - typically the local SQLite file into Postgres.

    python -m profiler_service.copy_db --from sqlite:///profiler.db --to postgresql+psycopg://user:pw@host/vayunx

* The source is only read (it is not migrated; columns it predates arrive as NULL).
* The target is migrated to the newest schema first and must be empty: nothing is merged or overwritten.
* Tables are copied parent-first in batches; row counts are checked afterwards; Postgres id sequences are
  moved past the copied ids so new rows keep getting unique ids.
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import create_engine, func, inspect, select, text

from profiler_service.db import make_engine
from profiler_service.models import Base

BATCH = 5000


class CopyError(RuntimeError):
    pass


def copy_database(source_url: str, target_url: str, log=lambda msg: None) -> dict:
    if source_url == target_url:
        raise CopyError("source and target are the same database")
    src = create_engine(source_url)
    dst = make_engine(target_url)  # creates / migrates the target schema
    src_tables = set(inspect(src).get_table_names())
    if "runs" not in src_tables:
        raise CopyError(f"the source has no Profiler tables: {source_url}")
    with dst.connect() as conn:
        busy = [t.name for t in Base.metadata.sorted_tables if conn.scalar(select(func.count()).select_from(t))]
    if busy:
        raise CopyError(f"the target is not empty (rows in {', '.join(busy)}); copy into a new, empty database")

    rows: dict[str, int] = {}
    with src.connect() as s, dst.begin() as d:
        for table in Base.metadata.sorted_tables:  # parents before children (foreign keys)
            if table.name not in src_tables:
                rows[table.name] = 0
                continue
            have = {c["name"] for c in inspect(src).get_columns(table.name)}
            cols = [c for c in table.columns if c.name in have]
            n = 0
            # selected through the model's columns, so values are converted by type (e.g. SQLite text -> datetime)
            result = s.execution_options(stream_results=True).execute(select(*cols))
            while batch := result.fetchmany(BATCH):
                d.execute(table.insert(), [dict(zip((c.name for c in cols), r)) for r in batch])
                n += len(batch)
            rows[table.name] = n
            log(f"{table.name}: {n} rows")
        if dst.dialect.name == "postgresql":
            for table in Base.metadata.sorted_tables:
                if "id" in table.columns and table.columns["id"].autoincrement is True:
                    d.execute(text(f"SELECT setval(pg_get_serial_sequence('{table.name}', 'id'), "
                                   f"COALESCE((SELECT MAX(id) FROM {table.name}), 0) + 1, false)"))

    with src.connect() as s, dst.connect() as d:
        for table in Base.metadata.sorted_tables:
            if table.name not in src_tables:
                continue
            a = s.scalar(select(func.count()).select_from(text(f'"{table.name}"')))
            b = d.scalar(select(func.count()).select_from(table))
            if a != b:
                raise CopyError(f"row count mismatch in {table.name}: source {a}, target {b}")
    src.dispose()
    dst.dispose()
    return {"source": _safe(source_url), "target": _safe(target_url), "rows": rows}


def _safe(url: str) -> str:
    """The URL without its password, for printing."""
    from sqlalchemy.engine import make_url
    return make_url(url).render_as_string(hide_password=True)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--from", dest="source", required=True, help="source database URL (read only)")
    p.add_argument("--to", dest="target", required=True, help="target database URL (must be empty)")
    a = p.parse_args(argv)
    try:
        report = copy_database(a.source, a.target, log=lambda m: print(m, file=sys.stderr))
    except CopyError as exc:
        print(f"copy_db: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
