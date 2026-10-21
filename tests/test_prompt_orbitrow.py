"""The Orbitrow store in SQLite and its create_table nodes read back (ddl.schema_from_sqlite), as the PoC's schema()."""
import sqlite3

import pytest

from fixtures.orbitrow.load import _seed, baseline, schema
from orbitrows.pipeline.prompt.pipeline import store_of
from orbitrows.sql.ddl import NewTable, check_references, ddl, schema_from_sqlite

AFFINITY = {"INTEGER": "INTEGER", "TEXT": "TEXT", "NUMERIC": "REAL", "REAL": "REAL"}   # the seed's declared types


def expected() -> list[dict]:
    """The PoC's schema() nodes, written from seed.json: ids t_/c_, NOT NULL or key → not nullable, RESTRICT, NO
    ACTION."""
    out, seed = [], _seed()["tables"]
    target_pk = lambda table: next(f"c_{c['name']}" for c in seed[table]["columns"] if c["primary_key"])
    for name, t in seed.items():
        pk = {c["name"]: f"c_{c['name']}" for c in t["columns"] if c["primary_key"]}
        out.append({"id": f"r_create_{name}", "type": "create_table", "definition": {
            "table_id": f"t_{name}", "sql_name": name,
            "columns": [{"column_id": f"c_{c['name']}", "sql_name": c["name"], "header": c["name"],
                         "sql_type": AFFINITY[c["type"]], "nullable": not (c["required"] or c["primary_key"]),
                         "default": None} for c in t["columns"]],
            "primary_key": list(pk.values()),
            "foreign_keys": [{"columns": [f"c_{col}"], "references": {"table": f"t_{to}", "columns": [target_pk(to)]},
                              "on_delete": "restrict", "on_update": "no_action"}
                             for col, to in reversed(t["foreign_keys"].items())]}})   # SQLite lists the last first
    return out


def test_nodes_match_the_seed():
    nodes = schema_from_sqlite(baseline())
    assert all(n.query is None for n in nodes)                # a store table is never filled from a query
    assert schema() == expected()


def test_orbitrow_self_check():
    """The PoC's load.py self-check: values kept exactly, references restricted, STRICT tables accept every row."""
    db = baseline()
    counts = {t: db.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in _seed()["tables"]}
    assert sum(counts.values()) == 74 and counts["variants"] == 17
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("SELECT barcode FROM variants WHERE sku = 'TEE-BLK-S'").fetchone() == ("000101",)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("DELETE FROM categories WHERE name = 'Clothing'")
    t = {n["definition"]["sql_name"]: n["definition"] for n in schema(db)}
    assert {c["sql_name"]: c["sql_type"] for c in t["variants"]["columns"]}["price_mad"] == "REAL"   # NUMERIC → REAL
    tables = [NewTable(**d) for d in t.values()]
    assert check_references(tables) == []
    strict, by_id = sqlite3.connect(":memory:"), {x.table_id: x for x in tables}
    for x in tables:
        strict.execute(ddl(x, by_id))
    for name, s in _seed()["tables"].items():
        strict.executemany(f'INSERT INTO "{name}" VALUES ({", ".join("?" * len(s["columns"]))})', s["rows"])


def test_store_leaves_out_the_cli_tables():
    db = baseline()
    db.execute('CREATE TABLE "_columns" (x TEXT)')            # the CLI's classification table
    assert store_of(db) == schema(baseline())
