from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from profiler_service.models import Base


MIGRATIONS = Path(__file__).resolve().parent / "migrations"
BASELINE = "0001"  # the schema every database had before Alembic (Phases 0-3)


def make_engine(db_url: str) -> Engine:
    kwargs = {"future": True}
    if db_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    else:
        kwargs["pool_pre_ping"] = True
    engine = create_engine(db_url, **kwargs)
    if db_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # enforce FOREIGN KEY constraints on SQLite
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    migrate(engine)
    return engine


def migrate(engine: Engine) -> str:
    """Bring the database to the newest schema with Alembic (profiler_service/migrations).

    A database created before Alembic (Phases 0-3: tables, no alembic_version) is first given any columns it
    lacks, then stamped at the baseline revision - its rows are kept. Returns the revision now in place."""
    from alembic import command
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext

    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    tables = set(inspect(engine).get_table_names())
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        if "runs" in tables and "alembic_version" not in tables:
            add_missing_columns(engine)
            command.stamp(cfg, BASELINE)
        command.upgrade(cfg, "head")
        return MigrationContext.configure(conn).get_current_revision()


def add_missing_columns(engine: Engine) -> list[str]:
    """Minimal forward migration: add columns that exist in the models but not in an existing table.

    Used only to bring a pre-Alembic database (Phases 0-3) up to the baseline before stamping it; schema
    changes from now on are Alembic revisions. Only nullable columns or columns with a server default can be
    added this way (existing rows get NULL / the default). Anything else raises instead of guessing.
    """
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    added = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have:
                    continue
                if not (col.nullable or col.server_default is not None):
                    raise RuntimeError(f"cannot add NOT NULL column {table.name}.{col.name} automatically")
                ddl = f"ALTER TABLE {table.name} ADD COLUMN {col.name} {col.type.compile(dialect=engine.dialect)}"
                if col.server_default is not None:
                    ddl += f" DEFAULT '{col.server_default.arg}'"
                conn.execute(text(ddl))
                added.append(f"{table.name}.{col.name}")
    return added


def make_sessionmaker(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)


def to_utc_naive(dt: datetime) -> datetime:
    """Store timestamps as naive UTC (portable across SQLite/Postgres)."""
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def iso_utc(dt: datetime | None) -> str | None:
    return None if dt is None else dt.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
