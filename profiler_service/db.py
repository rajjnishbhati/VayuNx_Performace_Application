from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from profiler_service.models import Base


MIGRATIONS = Path(__file__).resolve().parent / "migrations"


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
    if "runs" in tables and "alembic_version" not in tables:
        # pre-Alembic file (Phases 0-3): give it the full current schema - missing tables and columns - keep
        # every row, add the rows migrations would have seeded, and record it as up to date
        Base.metadata.create_all(engine)
        add_missing_columns(engine)
        seed_defaults(engine)
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.stamp(cfg, "head")
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")
        return MigrationContext.configure(conn).get_current_revision()


def seed_defaults(engine: Engine) -> None:
    """Rows the migrations create (keep in step with them): the Default project (revision 0003)."""
    from profiler_service.models import DEFAULT_PROJECT_ID, Project
    with engine.begin() as conn:
        if conn.execute(text("SELECT 1 FROM projects WHERE project_id = :p"), {"p": DEFAULT_PROJECT_ID}).first() is None:
            conn.execute(Project.__table__.insert().values(project_id=DEFAULT_PROJECT_ID, name="Default project",
                                                           default_role="editor", created_at=to_utc_naive(datetime.now(timezone.utc))))


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
