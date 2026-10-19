"""The registry (`stores.db`): the list of imported stores, nothing else (CLI plan §3.3)."""

import os
import secrets
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

FAILED, IMPORTING, READY = -1, 0, 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS store (
    id            TEXT PRIMARY KEY,           -- st_ + 8 hex characters; also the store's folder name
    name          TEXT NOT NULL,
    created_at    TEXT NOT NULL,              -- ISO 8601, UTC
    last_saved_at TEXT,                       -- empty until the first save
    status        INTEGER NOT NULL CHECK (status IN (-1, 0, 1))  -- -1 failed, 0 importing, 1 ready
) STRICT
"""


def home() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "pilotstore"


def connect() -> sqlite3.Connection:
    """`with connect() as db:` commits on success, rolls back on error."""
    home().mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(home() / "stores.db")
    db.execute(SCHEMA)
    return db


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def create(name: str) -> str:
    """A new store, status importing. Returns its id."""
    store_id = f"st_{secrets.token_hex(4)}"
    with closing(connect()) as db, db:
        db.execute("INSERT INTO store (id, name, created_at, status) VALUES (?, ?, ?, ?)",
                   (store_id, name, now(), IMPORTING))
    return store_id


def set_status(store_id: str, status: Literal[-1, 0, 1]) -> None:
    with closing(connect()) as db, db:
        db.execute("UPDATE store SET status = ? WHERE id = ?", (status, store_id))
