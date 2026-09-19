"""Alembic environment. Run through profiler_service.db.migrate() (the Service does this at start-up), or:
    alembic -c profiler_service/migrations/alembic.ini -x url=postgresql+psycopg://... upgrade head
"""

from alembic import context
from sqlalchemy import create_engine

from profiler_service.models import Base

target_metadata = Base.metadata


def run() -> None:
    connection = context.config.attributes.get("connection")
    if connection is not None:  # called from profiler_service.db.migrate()
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        return
    url = context.get_x_argument(as_dictionary=True).get("url") or context.config.get_main_option("sqlalchemy.url")
    engine = create_engine(url)
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


run()
