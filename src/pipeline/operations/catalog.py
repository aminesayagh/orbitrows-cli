"""Phase 2: the catalog, a SQLite file shipped with the CLI (operations.db), its single source of truth: no other
format is kept or imported. Opened read-only by the CLI; written only by a developer (save()), each change in one
transaction that raises the entry's version and the catalog's revision. Kept apart from every store's database.

    entry      one operation: what it does (behavior), what it is for (outcomes), how merchants ask for it
               (examples), its parameters, where it applies, how it differs from close ones (distinctions), its role
               (it chooses rows: condition; it computes a value: value; SQLGlot's own expressions: technical), the
               builder that makes our AST from its resolved parameters (builders.py), SQLGlot's class and names, and
               SQLite's support (the inventory's check, or why it is not built yet)
    enabled    only an entry whose builder is verified on the demo store (tests/test_operations.py: every enabled entry
               has its check); the inventory's entries stay disabled: no description, no builder yet
    embeddings phase 3 (the tables are here; the vectors are computed by the developer, never on the user's machine)

Schema: vault 02_projects/pilotstore/operation-selection-implementation, section 15, plus role, kind, sqlglot_class
and sql_names.
"""
import json
import sqlite3
from pathlib import Path

PATH = Path(__file__).with_name("operations.db")
ROLES = ("condition", "value", "technical")
KINDS = ("composition", "function", "aggregate", "window", "operator", "predicate")
SCHEMA = """
CREATE TABLE IF NOT EXISTS catalog_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    revision INTEGER NOT NULL DEFAULT 0 CHECK (revision >= 0)
);
INSERT OR IGNORE INTO catalog_state (singleton, revision) VALUES (1, 0);
CREATE TABLE IF NOT EXISTS operations (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    behavior TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('condition', 'value', 'technical')),
    effect TEXT NOT NULL CHECK (effect IN ('read', 'update', 'insert', 'delete', 'schema_change')),
    kind TEXT NOT NULL CHECK (kind IN ('composition', 'function', 'aggregate', 'window', 'operator', 'predicate')),
    sqlglot_class TEXT,
    sql_names_json TEXT NOT NULL DEFAULT '[]',
    builder_key TEXT,
    implementation_config_json TEXT NOT NULL DEFAULT '{}',
    applicability_json TEXT NOT NULL DEFAULT '[]',
    distinctions_json TEXT NOT NULL DEFAULT '[]',
    engine_support_json TEXT NOT NULL DEFAULT '[]',
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    enabled INTEGER NOT NULL DEFAULT 0 CHECK (enabled IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS operation_examples (
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    request_text TEXT NOT NULL,
    PRIMARY KEY (operation_id, position)
);
CREATE TABLE IF NOT EXISTS operation_outcomes (
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    position INTEGER NOT NULL CHECK (position >= 0),
    outcome_text TEXT NOT NULL,
    PRIMARY KEY (operation_id, position)
);
CREATE TABLE IF NOT EXISTS operation_parameters (
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    value_type TEXT NOT NULL,
    required INTEGER NOT NULL CHECK (required IN (0, 1)),
    default_json TEXT,
    constraints_json TEXT NOT NULL DEFAULT '{}',
    PRIMARY KEY (operation_id, name),
    UNIQUE (operation_id, position)
);
CREATE TABLE IF NOT EXISTS embedding_configs (
    id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    dimensions INTEGER NOT NULL CHECK (dimensions > 0),
    dtype TEXT NOT NULL CHECK (dtype = 'float32_le'),
    normalized INTEGER NOT NULL CHECK (normalized = 1),
    encoding_config_json TEXT NOT NULL,
    document_template_version INTEGER NOT NULL CHECK (document_template_version >= 1)
);
CREATE TABLE IF NOT EXISTS operation_embeddings (
    operation_id TEXT NOT NULL REFERENCES operations(id) ON DELETE CASCADE,
    embedding_config_id TEXT NOT NULL REFERENCES embedding_configs(id),
    operation_version INTEGER NOT NULL CHECK (operation_version >= 1),
    searchable_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    vector BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (operation_id, embedding_config_id)
);
"""
LISTS = {"outcomes": ("operation_outcomes", "outcome_text"), "examples": ("operation_examples", "request_text")}
JSON = {"sql_names": "sql_names_json", "config": "implementation_config_json", "applicability": "applicability_json",
        "distinctions": "distinctions_json", "engine_support": "engine_support_json"}
FIELDS = ("id", "name", "behavior", "role", "effect", "kind", "sqlglot_class", "builder_key", "enabled")


def connect(path: Path = PATH, write: bool = False) -> sqlite3.Connection:
    """The catalog; read-only unless a developer writes it (the schema is created then)."""
    if write:
        db = sqlite3.connect(path, isolation_level=None)
        db.execute("PRAGMA foreign_keys = ON")
        db.executescript(SCHEMA)
    else:
        db = sqlite3.connect(f"file:{path}?mode=ro", uri=True, isolation_level=None)
        db.execute("PRAGMA foreign_keys = ON")
    return db


def save(db, entry: dict) -> None:
    """Insert or replace one entry with its examples, outcomes and parameters, in one transaction: its version and
    the catalog's revision go up when anything changed (its vectors are then stale until recomputed)."""
    old = get(db, entry["id"])
    if old is not None and {k: v for k, v in old.items() if k != "version"} == canonical(entry):
        return
    db.execute("BEGIN")
    try:
        version = (old["version"] + 1) if old else 1
        db.execute("DELETE FROM operations WHERE id = ?", (entry["id"],))   # cascades to the lists and parameters
        cols = list(FIELDS) + list(JSON.values()) + ["version"]
        vals = [int(entry.get(f, 0)) if f == "enabled" else entry.get(f) for f in FIELDS] + \
               [json.dumps(entry.get(k, {} if k == "config" else []), ensure_ascii=False) for k in JSON] + [version]
        db.execute(f"INSERT INTO operations ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)
        for key, (table, column) in LISTS.items():
            db.executemany(f"INSERT INTO {table} (operation_id, position, {column}) VALUES (?, ?, ?)",
                           [(entry["id"], i, t) for i, t in enumerate(entry.get(key, []))])
        db.executemany("INSERT INTO operation_parameters VALUES (?, ?, ?, ?, ?, ?, ?)",
                       [(entry["id"], p["name"], i, p["type"], int(p.get("required", True)),
                         json.dumps(p["default"]) if "default" in p else None,
                         json.dumps(p.get("constraints", {}), ensure_ascii=False))
                        for i, p in enumerate(entry.get("parameters", []))])
        db.execute("UPDATE catalog_state SET revision = revision + 1")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def canonical(entry: dict) -> dict:
    """An entry as get() returns it, without its version: what save() compares."""
    return {**{f: (int(entry.get(f, 0)) if f == "enabled" else entry.get(f)) for f in FIELDS},
            **{k: entry.get(k, {} if k == "config" else []) for k in JSON},
            **{k: list(entry.get(k, [])) for k in LISTS},
            "parameters": [{"name": p["name"], "type": p["type"], "required": bool(p.get("required", True)),
                            **({"default": p["default"]} if "default" in p else {}),
                            "constraints": p.get("constraints", {})} for p in entry.get("parameters", [])]}


def get(db, operation_id: str) -> dict | None:
    row = db.execute(f"SELECT {', '.join(FIELDS)}, {', '.join(JSON.values())}, version FROM operations WHERE id = ?",
                     (operation_id,)).fetchone()
    if row is None:
        return None
    out = dict(zip(FIELDS, row)) | {k: json.loads(v) for k, v in zip(JSON, row[len(FIELDS):-1])} | {"version": row[-1]}
    for key, (table, column) in LISTS.items():
        out[key] = [t for (t,) in db.execute(f"SELECT {column} FROM {table} WHERE operation_id = ? ORDER BY position",
                                             (operation_id,))]
    out["parameters"] = [{"name": n, "type": t, "required": bool(r), **({"default": json.loads(d)} if d is not None
                                                                         else {}), "constraints": json.loads(c)}
                         for n, t, r, d, c in db.execute(
                             "SELECT name, value_type, required, default_json, constraints_json FROM "
                             "operation_parameters WHERE operation_id = ? ORDER BY position", (operation_id,))]
    return out


def entries(db, enabled: bool | None = True) -> list[dict]:
    where = "" if enabled is None else f" WHERE enabled = {int(enabled)}"
    return [get(db, i) for (i,) in db.execute(f"SELECT id FROM operations{where} ORDER BY id")]


def problems(db) -> list[str]:
    """What the catalog must never ship with: a broken reference, an enabled entry without a builder, a description,
    examples or parameters."""
    from .builders import BUILDERS
    out = [f"foreign key: {r}" for r in db.execute("PRAGMA foreign_key_check")]
    for e in entries(db):
        if e["builder_key"] not in BUILDERS:
            out.append(f"{e['id']}: builder {e['builder_key']!r} is not registered")
        if not e["examples"] or not e["behavior"] or not e["outcomes"]:
            out.append(f"{e['id']}: enabled without behavior, outcomes or examples")
        if e["role"] == "technical":
            out.append(f"{e['id']}: a technical expression is never enabled on its own")
    return out


def inventoried(db) -> int:
    """The inventory's expressions as disabled technical entries (SQL_<class>), SQLite's verdict in engine_support.
    → how many were saved."""
    from sqlite3 import sqlite_version
    from .inventory import inventory
    n = 0
    for r in inventory():
        save(db, {"id": f"SQL_{r['class'].upper()}", "name": r["names"][0], "behavior": "", "role": "technical",
                  "effect": "read", "kind": r["kind"], "sqlglot_class": r["class"], "sql_names": r["names"],
                  "parameters": [{"name": a, "type": "expression", "required": req} for a, req in r["arguments"].items()],
                  "engine_support": [{"engine": "sqlite", "version": sqlite_version, "runs": bool(r["sqlite"]),
                                      **({"sql": r["sqlite"]["sql"], "window": r["sqlite"]["window"]}
                                         if r["sqlite"] else {})}]})
        n += 1
    return n
