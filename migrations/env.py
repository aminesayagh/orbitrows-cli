"""Alembic environment: URL from DATABASE_URL, schema from orbitrows.services.db.models."""

from logging.config import fileConfig

from alembic import context

from orbitrows.services.db import connect
from orbitrows.services.db.models import Base

if context.config.config_file_name:
    fileConfig(context.config.config_file_name)

# ponytail: online mode only; add run_migrations_offline when SQL scripts (--sql) are needed
with connect().connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
