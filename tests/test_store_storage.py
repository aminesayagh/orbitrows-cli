import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from orbitrows.pipeline.store import run
from orbitrows.services.storage import store as storage

CASES = Path(__file__).parent / "fixtures" / "cases"
EXPECTED = json.loads((CASES / "store_schema_expected.json").read_text())
OWN = {"_store_tables", "_column_context", "_store_columns", "_export", "_history"}


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))


def case(name):
    """The store files' saved sections (as test_store_schema builds them) and their rows."""
    saved = json.loads((CASES / name / "result.json").read_text())
    files = {f: s for f, s in saved["tables"].items() if f != "incoming.csv"}
    return files, run.rows([CASES / name / f for f in files], files)


def tables(path) -> dict:
    with closing(sqlite3.connect(path)) as db:
        names = [n for n, in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {n: db.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}


@pytest.mark.parametrize("name", ["shop", "joins_chain"])
def test_store_is_written(name):
    files, rows = case(name)
    built = run.build(files)
    st = built["store"]
    counts = storage.write("st_test", st, rows)
    folder = storage.folder("st_test")
    db = sqlite3.connect(folder / "session.db")
    defs = {t["definition"]["table_id"]: t["definition"] for t in st["tables"]}
    loaded = tables(folder / "session.db")
    assert set(loaded) == {d["sql_name"] for d in defs.values()} | OWN
    for ex in st["export"]:                                   # a root table: one record per row
        assert loaded[defs[ex["root"]]["sql_name"]] == len(rows[ex["file"]])
        for t_id in {c["table_id"] for c in ex["columns"]} - {ex["root"]}:   # a group table: one per distinct key
            d = defs[t_id]
            key = next(c["header"] for c in d["columns"] if c["column_id"] == d["primary_key"][0])
            i = [c["header"] for c in ex["columns"]].index(key)
            assert loaded[d["sql_name"]] == len({r[i] for r in rows[ex["file"]] if i < len(r) and r[i]}), d["sql_name"]
    assert {c["table"]: c["rows"] for c in counts} == {n: k for n, k in loaded.items() if n not in OWN}
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)

    got = db.execute("SELECT c.id, t.sql_name, c.sql_name, c.role, c.kind, c.level, c.unit_id, c.context_column_id, "
                     "c.source_header, c.position FROM _store_columns c JOIN _store_tables t ON t.id = c.table_id "
                     "ORDER BY c.id").fetchall()
    assert len(got) == len(st["metadata"])
    index = lambda names, v: None if v is None else names.index(v)
    for row, m in zip(got, st["metadata"]):
        sql = next(c["sql_name"] for c in defs[m["table_id"]]["columns"] if c["column_id"] == m["column_id"])
        assert row[1:6] == (defs[m["table_id"]]["sql_name"], sql, index(storage.ROLES, m["role"]),
                            index(storage.KINDS, m["kind"]), index(storage.LEVELS, m["level"]))
        assert row[8:] == (m["source_header"], m["position"])
        assert (row[6] is None) == (m["unit_kind"] is None) and (row[7] is None) == (m["context_column_id"] is None)
    ids = {r[0] for r in got}
    units = {u for u, in db.execute("SELECT id FROM _column_context")}
    assert {r[6] for r in got} - {None} == units and {r[7] for r in got} - {None} <= ids

    export = db.execute("SELECT file, has_header, root_table_id, spec_json FROM _export").fetchall()
    table_ids = {i for i, in db.execute("SELECT id FROM _store_tables")}
    assert sorted(e[0] for e in export) == sorted(files)
    for file, has_header, root, spec in export:
        spec = json.loads(spec)
        assert root in table_ids and has_header == int(files[file]["read"]["has_header"])
        assert [c["header"] for c in spec] == files[file]["read"]["header"]
        assert all(c["table_id"] in table_ids and c["column_id"] in ids and set(c["via"]) <= ids for c in spec)

    for (header, profile), m in zip(db.execute("SELECT source_header, profile_json FROM _store_columns ORDER BY id"),
                                    st["metadata"]):            # each column's own profile; none for an added id
        want = None if header is None else files[m["source_file"]]["profile"]["columns"][header]
        assert (profile and json.loads(profile)) == want, header

    [(hid, name, prompt, ast, sql, done)] = db.execute(
        "SELECT id, name, prompt, json_ast, sql, classification_done FROM _history").fetchall()
    assert (hid, name, prompt, done) == (1, "Import", None, 1) and json.loads(ast) == st["tables"]
    sql = json.loads(sql)
    assert len(sql) == len(defs) and all(s.startswith("CREATE TABLE") for s in sql)
    created = {n for n, in db.execute("SELECT sql FROM sqlite_master WHERE type = 'table'")}
    assert set(sql) <= created                                # exactly the statements that made the tables
    db.close()
    assert tables(folder / "original.db") == loaded


def test_a_value_lost_on_the_way_refuses_the_store():
    files, rows = case("joins_chain")
    st = run.build(files)["store"]
    rows["export.csv"][1][7] = "Alice M."                      # customer 1's name now disagrees with row 0's
    with pytest.raises(storage.Lost) as lost:
        storage.write("st_test", st, rows)
    assert lost.value.diffs == [{"file": "export.csv", "row": 1, "header": "customer_name", "uploaded": "Alice M.",
                                 "rebuilt": "Alice Martin", "kind": "value"}]
    assert sorted(p.name for p in storage.folder("st_test").iterdir()) == []


def test_a_cell_beyond_the_header_refuses_the_store(tmp_path):
    for f in (CASES / "shop").glob("*.csv"):
        shutil.copy(f, tmp_path / f.name)
    lines = (tmp_path / "customers.csv").read_text().splitlines()
    lines[1] += ",LOST CELL"                                  # row 1 has one cell more than the header
    (tmp_path / "customers.csv").write_text("\n".join(lines) + "\n")
    files = json.loads((CASES / "shop" / "result.json").read_text())["tables"]
    rows = run.rows([tmp_path / f for f in files], files)
    with pytest.raises(storage.Lost) as lost:
        storage.write("st_test", run.build(files)["store"], rows)
    assert lost.value.diffs == [{"file": "customers.csv", "row": 0, "header": None, "uploaded": "LOST CELL",
                                 "rebuilt": "", "kind": "value"}]
    assert sorted(p.name for p in storage.folder("st_test").iterdir()) == []


def test_files_are_copied_unchanged():
    paths = sorted((CASES / "shop").glob("*.csv"))
    storage.copy_files("st_test", paths)
    for p in paths:
        assert (storage.folder("st_test") / "files" / p.name).read_bytes() == p.read_bytes()


def test_the_stored_integers_are_fixed():
    assert storage.ROLES == ("dedicated_key", "descriptive_base", "qualifier", "none")
    assert storage.KINDS == ("record_reference", "model_reference", "name", "attribute", "contact_or_address",
                             "value_or_text")
    assert storage.LEVELS == ("individual_record", "product_family")
