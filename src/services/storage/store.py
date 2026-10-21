"""A store on disk (CLI plan §3.2, §3.4, §4.1): `stores/<id>/` under the registry's home.

    files/        the uploaded CSV files, unchanged
    session.db    the store's tables with their rows (one per table of the import's schema), the classification
                  tables below and the history, whose first row is the import; written in one transaction, kept only when every file rebuilds from it
    original.db   a copy of session.db right after the import (sqlite3 backup API)
    import.json   the import's progress: the files, every model answer (Recorded's calls, with their probabilities),
                  the open and answered questions, the notices already shown; kept once the import is done
    import.lock   empty; locked (flock) while an `answer` runs, so a second one on the store waits for it
"""
import fcntl
import json
import os
import shutil
import sqlite3
from contextlib import closing, contextmanager
from pathlib import Path

from orbitrows.pipeline.store import roles
from orbitrows.pipeline.store.schema import check, load, nodes, q, table_at
from orbitrows.services.storage import registry
from orbitrows.sql.ddl import ddl

# the stored integer is the index (user decision 2026-10-21); the PoC's order, which is roles.py's
ROLES = tuple(dict.fromkeys(roles.KIND_ROLE.values()))
KINDS = tuple(roles.KINDS)
LEVELS = tuple(roles.LEVELS)

# The plan's store_tables, column_context, store_columns, export and history, with a leading _: a store table is named after its
# file (export.csv → export) and sql_name() never starts a name with _, so the two can never collide.
# ponytail: no created_by/edited_by yet: empty means the import, and only the import writes for now
SCHEMA = f"""
CREATE TABLE _store_tables (
    id          INTEGER PRIMARY KEY,
    sql_name    TEXT NOT NULL UNIQUE,
    source_file TEXT,
    created_at  TEXT NOT NULL                          -- ISO 8601, UTC
) STRICT;
CREATE TABLE _column_context (
    id        INTEGER PRIMARY KEY,
    unit      TEXT,                                    -- NULL: it varies row by row (a context column, or a unit given by one)
    unit_kind TEXT NOT NULL,
    unit_from TEXT CHECK (unit_from IN ('header', 'column'))
) STRICT;
CREATE TABLE _store_columns (
    id                INTEGER PRIMARY KEY,
    table_id          INTEGER NOT NULL REFERENCES _store_tables(id),
    sql_name          TEXT NOT NULL,
    sql_type          TEXT NOT NULL,
    source_header     TEXT,
    position          INTEGER,
    role              INTEGER CHECK (role BETWEEN 0 AND {len(ROLES) - 1}),
    kind              INTEGER CHECK (kind BETWEEN 0 AND {len(KINDS) - 1}),
    level             INTEGER CHECK (level BETWEEN 0 AND {len(LEVELS) - 1}),
    unit_id           INTEGER REFERENCES _column_context(id),
    context_column_id INTEGER REFERENCES _store_columns(id),
    state             TEXT NOT NULL DEFAULT 'current' CHECK (state IN ('current', 'stale')),
    extra_json        TEXT,
    profile_json      TEXT CHECK (json_valid(profile_json)),  -- the import's profile: empty, distinct, max_repeat, values…
    UNIQUE (table_id, sql_name)
) STRICT;
CREATE TABLE _export (
    file          TEXT PRIMARY KEY,
    has_header    INTEGER NOT NULL CHECK (has_header IN (0, 1)),
    root_table_id INTEGER NOT NULL REFERENCES _store_tables(id),
    spec_json     TEXT NOT NULL                        -- the file's columns: header, table_id, column_id, via (ids above)
) STRICT;
CREATE TABLE _history (
    id                    INTEGER PRIMARY KEY,         -- the order of the changes, 1 is the import
    created_at            TEXT NOT NULL,               -- ISO 8601, UTC
    name                  TEXT NOT NULL,
    prompt                TEXT,                        -- the client's sentence, NULL for the import
    json_ast              TEXT NOT NULL CHECK (json_valid(json_ast)),
    sql                   TEXT NOT NULL CHECK (json_valid(sql)),  -- the statements run, as a JSON array
    reclassification_json TEXT CHECK (json_valid(reclassification_json)),
    classification_done   INTEGER NOT NULL CHECK (classification_done IN (0, 1))
) STRICT;
"""


class Lost(Exception):
    """The store's tables do not rebuild the uploaded files: nothing was written."""

    def __init__(self, diffs):
        super().__init__(f"{len(diffs)} differences")
        self.diffs = diffs


def folder(store_id: str) -> Path:
    return registry.home() / "stores" / store_id


def load_import(store_id: str) -> dict:
    """import.json: {"files": [names, upload order], "calls", "step", "open": [questions], "answered", "shown"}."""
    return json.loads((folder(store_id) / "import.json").read_text())


@contextmanager
def locked(store_id: str):
    """The store's import.lock, held until the block ends: another holder, in any process, waits for it."""
    with open(folder(store_id) / "import.lock", "w") as f:  # closing the file releases the lock, a kill too
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def save_import(store_id: str, progress: dict) -> None:
    """import.json, atomically: a crash leaves the old file or the new one, never half of one."""
    path = folder(store_id) / "import.json"
    temp = path.with_name("import.json.tmp")
    temp.write_text(json.dumps(progress, ensure_ascii=False))
    os.replace(temp, path)


def copy_files(store_id: str, paths: list) -> None:
    """The uploaded files into files/, same names, bytes unchanged."""
    target = folder(store_id) / "files"
    target.mkdir(parents=True, exist_ok=True)
    for p in paths:
        shutil.copyfile(p, target / Path(p).name)


def _index(names: tuple, value):
    return None if value is None else names.index(value)


def _classify(db, store: dict) -> None:
    """The classification tables, from the import's metadata and export map; no import id is left in them."""
    defs = {t["definition"]["table_id"]: t["definition"] for t in store["tables"]}
    table_ids = {t_id: n for n, t_id in enumerate(defs, 1)}
    column_ids = {(m["table_id"], m["column_id"]): n for n, m in enumerate(store["metadata"], 1)}
    files = {m["table_id"]: m["source_file"] for m in store["metadata"] if m["source_file"]}
    now = registry.now()
    db.executemany("INSERT INTO _store_tables VALUES (?, ?, ?, ?)",
                   [(n, defs[t_id]["sql_name"], files.get(t_id), now) for t_id, n in table_ids.items()])
    units = [m for m in store["metadata"] if m["unit_kind"] is not None]
    unit_ids = {id(m): n for n, m in enumerate(units, 1)}
    db.executemany("INSERT INTO _column_context VALUES (?, ?, ?, ?)",
                   [(unit_ids[id(m)], m["unit"], m["unit_kind"], m["unit_from"]) for m in units])
    columns = {(t_id, c["column_id"]): c for t_id, d in defs.items() for c in d["columns"]}
    db.executemany(
        "INSERT INTO _store_columns (id, table_id, sql_name, sql_type, source_header, position, role, kind, level, "
        "unit_id, context_column_id, extra_json, profile_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [(column_ids[m["table_id"], m["column_id"]], table_ids[m["table_id"]],
          (c := columns[m["table_id"], m["column_id"]])["sql_name"], c["sql_type"], m["source_header"], m["position"],
          _index(ROLES, m["role"]), _index(KINDS, m["kind"]), _index(LEVELS, m["level"]), unit_ids.get(id(m)),
          column_ids.get((m["context_table_id"], m["context_column_id"])), m["extra_json"],
          None if m.get("profile") is None else json.dumps(m["profile"], ensure_ascii=False)) for m in store["metadata"]])
    tables = nodes(store)
    db.execute("INSERT INTO _history (created_at, name, json_ast, sql, classification_done) VALUES (?, ?, ?, ?, 1)",
               (now, "Import", json.dumps(store["tables"], ensure_ascii=False),
                json.dumps([ddl(t, tables) for t in tables.values()], ensure_ascii=False)))   # what load() ran
    for ex in store["export"]:                                # via: the reference columns, each in the table it leaves
        spec = [{"header": c["header"], "table_id": table_ids[c["table_id"]],
                 "column_id": column_ids[c["table_id"], c["column_id"]],
                 "via": [column_ids[table_at(tables, ex["root"], c["via"][:i]), k] for i, k in enumerate(c["via"])]}
                for c in ex["columns"]]
        db.execute("INSERT INTO _export VALUES (?, ?, ?, ?)", (ex["file"], int(ex["has_header"]), table_ids[ex["root"]],
                                                             json.dumps(spec, ensure_ascii=False)))


def write(store_id: str, store: dict, rows: dict) -> list[dict]:
    """session.db, then original.db, from build()'s store and the files' rows ({"<file>.csv": rows}).
    → [{"table", "file", "rows", "columns"}], one per store table. Raises Lost when a file does not rebuild cell for
    cell (a "number_format" difference is accepted); on any failure neither database is left behind."""
    session, original = folder(store_id) / "session.db", folder(store_id) / "original.db"
    session.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(sqlite3.connect(session, isolation_level=None)) as db:
            db.execute("PRAGMA foreign_keys = ON")
            db.execute("PRAGMA synchronous = FULL")
            db.execute("PRAGMA journal_mode = DELETE")
            db.execute("BEGIN")
            db.execute("PRAGMA defer_foreign_keys = ON")      # a row may reference a group record loaded after it
            diffs = load(db, store, rows)
            for sql in SCHEMA.split(";")[:-1]:              # not executescript: it would COMMIT first
                db.execute(sql)
            _classify(db, store)
            diffs += check(db, store, rows)                   # the proof, on this file, before COMMIT
            if lost := [d for d in diffs if d["kind"] == "value"]:
                db.execute("ROLLBACK")
                raise Lost(lost)
            db.execute("COMMIT")
            if (ok := db.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
                raise sqlite3.DatabaseError(f"session.db quick_check: {ok}")
            files = dict(db.execute("SELECT sql_name, source_file FROM _store_tables"))
            counts = [{"table": n, "file": files[n], "rows": db.execute(f"SELECT COUNT(*) FROM {q(n)}").fetchone()[0],
                       "columns": len(t["definition"]["columns"])}
                      for t in store["tables"] for n in [t["definition"]["sql_name"]]]
            with closing(sqlite3.connect(original)) as copy:
                db.backup(copy)
    except BaseException:
        for p in (session, session.with_name("session.db-journal"), original):
            p.unlink(missing_ok=True)
        raise
    return counts
