"""The PoC's Orbitrow store (v2/datasets/orbitrow/load.py at 8c923a8): baseline() and schema(), unchanged; the
prompts and the self-check are left out (the self-check is in tests/test_prompt_orbitrow.py)."""
import json
import sqlite3
from functools import cache
from pathlib import Path

from orbitrows.sql.ddl import schema_from_sqlite

HERE = Path(__file__).parent


@cache
def _seed() -> dict:
    return json.loads((HERE / "seed.json").read_text())


def baseline(path=":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    for name, t in _seed()["tables"].items():
        cols = [f'"{c["name"]}" {c["type"]}{" NOT NULL" if c["required"] else ""}{" PRIMARY KEY" if c["primary_key"] else ""}'
                for c in t["columns"]]
        pk = {c["name"] for c in t["columns"] if c["primary_key"]}
        cols += [f'FOREIGN KEY ("{col}") REFERENCES "{target}" ("{next(iter(_pk(target)))}") ON DELETE RESTRICT'
                 for col, target in t["foreign_keys"].items()]
        assert len(pk) == 1, name
        db.execute(f'CREATE TABLE "{name}" ({", ".join(cols)})')
    for name, t in _seed()["tables"].items():                 # seed order respects the references
        db.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(t["columns"]))})', t["rows"])
    db.commit()
    return db


def _pk(table: str) -> set:
    return {c["name"] for c in _seed()["tables"][table]["columns"] if c["primary_key"]}


def schema(db: sqlite3.Connection | None = None) -> list[dict]:
    return [n.model_dump() for n in schema_from_sqlite(db or baseline())]
