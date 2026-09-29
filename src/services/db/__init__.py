"""PostgreSQL access (schema in models.py, migrations in migrations/)."""

import os
from functools import cache

from sqlalchemy import Engine, create_engine


@cache  # one engine (and connection pool) per process
def connect() -> Engine:
    return create_engine(os.environ["DATABASE_URL"])
