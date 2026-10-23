"""Immutable incoming CSV snapshots in the store's session.db."""

import json
import os
import secrets
import shutil
import sqlite3
from pathlib import Path

from orbitrows.pipeline.store.schema import q
from orbitrows.services.storage import registry, store
from orbitrows.sql.ddl import CreateTable, ddl, sql_name


SCHEMA = """
CREATE TABLE IF NOT EXISTS _incoming_tables (
    id INTEGER PRIMARY KEY,
    sql_name TEXT NOT NULL UNIQUE,
    source_file TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    created_by INTEGER NOT NULL REFERENCES _history(id)
) STRICT;
CREATE TABLE IF NOT EXISTS _column_context (
    id INTEGER PRIMARY KEY,
    unit TEXT,
    unit_kind TEXT NOT NULL,
    unit_from TEXT CHECK (unit_from IN ('header', 'column'))
) STRICT;
CREATE TABLE IF NOT EXISTS _incoming_columns (
    id INTEGER PRIMARY KEY,
    table_id INTEGER NOT NULL REFERENCES _incoming_tables(id),
    sql_name TEXT NOT NULL,
    sql_type TEXT NOT NULL,
    source_header TEXT,
    position INTEGER NOT NULL,
    role INTEGER,
    kind INTEGER,
    level INTEGER,
    unit_id INTEGER REFERENCES _column_context(id),
    context_column_id INTEGER REFERENCES _incoming_columns(id),
    extra_json TEXT,
    UNIQUE (table_id, sql_name)
) STRICT;
CREATE TABLE IF NOT EXISTS _incoming_store_matches (
    incoming_column_id INTEGER NOT NULL REFERENCES _incoming_columns(id),
    store_column_id INTEGER NOT NULL REFERENCES _store_columns(id),
    relation TEXT NOT NULL CHECK (relation IN ('direct', 'partial')),
    confidence REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    created_at TEXT NOT NULL,
    PRIMARY KEY (incoming_column_id, store_column_id)
) STRICT;
CREATE INDEX IF NOT EXISTS _incoming_store_matches_store_column
    ON _incoming_store_matches(store_column_id);
"""


def pending_dir(store_id: str, upload_id: str) -> Path:
    return store.folder(store_id) / "incoming_pending" / upload_id


def start(store_id: str, path: Path, encoding: str | None = None) -> tuple[str, dict]:
    """Keep an upload's bytes and question answers across CLI processes."""
    upload_id = "up_" + secrets.token_hex(6)
    directory = pending_dir(store_id, upload_id)
    directory.mkdir(parents=True)
    shutil.copyfile(path, directory / path.name)
    progress = {"file": path.name, "encoding": encoding, "calls": {}, "open": [],
                "answered": [], "shown": [], "step": None}
    save_pending(store_id, upload_id, progress)
    return upload_id, progress


def save_pending(store_id: str, upload_id: str, progress: dict) -> None:
    destination = pending_dir(store_id, upload_id) / "progress.json"
    temp = destination.with_name("progress.json.tmp")
    temp.write_text(json.dumps(progress, ensure_ascii=False))
    os.replace(temp, destination)


def load_pending(store_id: str, upload_id: str) -> dict:
    return json.loads((pending_dir(store_id, upload_id) / "progress.json").read_text())


def pending_path(store_id: str, upload_id: str, progress: dict) -> Path:
    return pending_dir(store_id, upload_id) / progress["file"]


def save_classified(store_id: str, upload_id: str, table: dict, saved: dict) -> Path:
    """The classified upload, for the store's session to write: → its pending folder."""
    directory = pending_dir(store_id, upload_id)
    (directory / "classified.json").write_text(json.dumps({"table": table, "saved": saved}, ensure_ascii=False,
                                                          default=str))
    return directory


def upload(db: sqlite3.Connection, root: Path, folder: Path) -> dict:
    """A classified pending upload (folder) written on the session's connection, inside its open savepoint: the
    table, its rows, metadata, matches, read-only triggers and its _history row. The CSV stays in folder until the
    session commits (place()). → {"id", "table", "source_file", "rows", "columns", "name", "sql"}."""
    file = json.loads((folder / "progress.json").read_text())["file"]
    classified = json.loads((folder / "classified.json").read_text())
    table, saved = classified["table"], classified["saved"]
    for section, by in (("profile", "columns"), ("roles", "columns"), ("context", "context_columns"),
                        ("context", "source_context")):  # keyed by position: JSON made the keys str
        saved[section][by] = {int(i): v for i, v in saved[section][by].items()}
    statement = ""
    for line in SCHEMA.splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            db.execute(statement)
            statement = ""
    table_id = db.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM _incoming_tables").fetchone()[0]
    old_ids = [int(p.name) for p in (root / "incoming").iterdir() if p.is_dir() and p.name.isdigit()] \
        if (root / "incoming").is_dir() else []
    table_id = max(table_id, max(old_ids, default=0) + 1)
    # create_table() normalizes identifiers, so use a name it preserves exactly.
    name = f"in_{sql_name(Path(file).stem, default='imported')}_{table_id}"
    source_ast = CreateTable(**saved["schema"]["create_table"])
    definition = source_ast.definition.model_copy(update={"sql_name": name})
    # A candidate PK is enforced only when the whole file proves it unique and nonempty.
    if definition.primary_key:
        key = definition.primary_key[0]
        position = next(i for i, c in enumerate(definition.columns) if c.column_id == key)
        facts = saved["profile"]["columns"][position]
        if facts["empty"] or facts["max_repeat"] != 1:
            definition = definition.model_copy(update={"primary_key": []})
    ast = CreateTable(id=f"r_create_{name}", definition=definition)
    statements = [ddl(definition)] + [f'CREATE TRIGGER "_incoming_no_{operation.lower()}_{table_id}" '
                                      f'BEFORE {operation} ON {q(name)} '
                                      "BEGIN SELECT RAISE(FAIL, 'Incoming tables are read-only'); END"
                                      for operation in ("INSERT", "UPDATE", "DELETE")]
    change = f"Upload {file}"
    history = db.execute("INSERT INTO _history (created_at, name, prompt, json_ast, sql, classification_done) "
                         "VALUES (?, ?, NULL, ?, ?, 1)",
                         (registry.now(), change, json.dumps([ast.model_dump(exclude_none=True)], ensure_ascii=False),
                          json.dumps(statements, ensure_ascii=False))).lastrowid
    source_file = f"incoming/{table_id}/{file}"
    db.execute("INSERT INTO _incoming_tables VALUES (?, ?, ?, ?, ?)",
               (table_id, name, source_file, registry.now(), history))
    db.execute(statements[0])
    columns = definition.columns
    db.executemany(f"INSERT INTO {q(name)} VALUES ({', '.join('?' for _ in columns)})", table["rows"])
    column_ids = {}
    for i, c in enumerate(columns):
        role = saved["roles"]["columns"].get(i, {})
        context = saved["context"]
        own = context["context_columns"].get(i)
        source = context["source_context"].get(i)
        unit = own or source
        unit_id = None
        if unit:
            unit_id = db.execute("INSERT INTO _column_context (unit, unit_kind, unit_from) VALUES (?, ?, ?)",
                                 (unit.get("unit"), unit["kind"], unit.get("from"))).lastrowid
        extra = {"profile": saved["profile"]["columns"][i]}
        if role.get("flagged"):
            extra["flagged"] = True
        if own:
            extra["context"] = own["context"]
            extra["describes"] = own["describes"]
        if c.column_id in source_ast.definition.primary_key:
            extra["primary_key_candidate"] = True
        column_ids[i] = db.execute(
            "INSERT INTO _incoming_columns (table_id, sql_name, sql_type, source_header, position, "
            "role, kind, level, unit_id, extra_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (table_id, c.sql_name, c.sql_type, c.header, i,
             store._index(store.ROLES, role.get("role")), store._index(store.KINDS, role.get("kind")),
             store._index(store.LEVELS, role.get("level")), unit_id,
             json.dumps(extra, ensure_ascii=False))).lastrowid
    for i, source in saved["context"]["source_context"].items():
        if source["from"] == "column":
            db.execute("UPDATE _incoming_columns SET context_column_id = ? WHERE id = ?",
                       (column_ids[source["column"]], column_ids[i]))
    db.executemany("INSERT INTO _incoming_store_matches VALUES (?, ?, ?, ?, ?)",
                   [(column_ids[m["incoming_position"]], m["store_column_id"], m["relation"],
                     m["confidence"], registry.now()) for m in saved.get("matching", {}).get("matches", [])])
    for trigger in statements[1:]:
        db.execute(trigger)
    return {"id": table_id, "table": name, "source_file": source_file, "rows": len(table["rows"]),
            "columns": len(columns), "name": change, "sql": statements}


def place(root: Path, folder: Path, source_file: str) -> None:
    """Once the session committed the upload: its CSV moved from the pending folder to source_file, the folder
    removed."""
    destination = root / source_file
    destination.parent.mkdir(parents=True, exist_ok=False)
    os.replace(folder / destination.name, destination)
    shutil.rmtree(folder)
    # Earlier development versions stored incoming tables in a separate file. The user chose
    # a fresh start; copied CSV bytes stay under incoming/ so they can be reimported if needed.
    (root / "incoming.db").unlink(missing_ok=True)
