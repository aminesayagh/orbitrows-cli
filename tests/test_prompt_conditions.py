"""Step 6 on a linked table, with stubbed model answers: a column compared with a linked table's, and a matching
record there or none. No model loaded."""
import sqlite3

import pytest

from orbitrows.pipeline.prompt import assemble, conditions, grouping
from orbitrows.sql.ddl import schema_from_sqlite


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setattr(conditions, "header_unit", lambda header: None)    # GLiNER2: never loaded
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE variants (sku TEXT PRIMARY KEY, price REAL)")
    db.execute("CREATE TABLE offer (sku TEXT PRIMARY KEY REFERENCES variants (sku), recommended REAL)")
    db.executemany("INSERT INTO variants VALUES (?, ?)", [("A", 10), ("B", 20), ("C", 30), (None, 5)])
    db.executemany("INSERT INTO offer VALUES (?, ?)", [("A", 8), ("B", 25), ("C", 30), ("E", 1), ("F", 2)])
    return db, [n.model_dump() for n in schema_from_sqlite(db)]


def built(db, schema, text, main, stage_1, stage_2) -> list:
    """Steps 6 and 7 for one read of `main` (every column shown) → the rows it returns."""
    other = {"t_variants": "t_offer", "t_offer": "t_variants"}[main]
    requests = [{"id": "r1", "text": text, "statement": "select"}]
    sources = {"r1": {"from": {"type": "database_table", "reference": main}, "reads": [other]}}
    section = conditions.build(requests, schema, sources, [], stage_1)
    conditions.build_2(db, requests, schema, section["requests"], [], {"r1": []}, {}, stage_2, section)
    tree = grouping.build(requests, section, {}, {})["requests"]
    out = assemble.assemble(db, schema, requests, {"r1": {"statement": "select"}}, sources, {"r1": {"outputs": "all"}},
                            section["requests"], tree)
    assert not out["errors"], out
    return sorted(out["program"][0]["returns"])


def test_a_column_compared_with_a_linked_table_s(store):
    db, schema = store
    text = "variants whose offer recommended price is below the price"
    requests = [{"id": "r1", "text": text, "statement": "select"}]
    asked = conditions.questions_2(db, requests, schema, {"r1": {"columns": ["t_variants.c_price"]}}, [], {"r1": []}, {})
    assert "c:t_offer.c_recommended" in asked["r1|t_variants.c_price|value"]["criteria"]
    key = "r1|t_variants.c_price"
    rows = built(db, schema, text, "t_variants", {"r1_all": 0.02, "r1_chooses_t_variants.c_price": 0.97},
                 {f"{key}|op": (">", {">": 0.95}), f"{key}|upper": ("none", {"none": 0.95}), f"{key}|not": 0.02,
                  f"{key}|value": ("c:t_offer.c_recommended", {"c:t_offer.c_recommended": 0.95})})
    assert rows == [["A", 10.0]]                              # recommended 8 < 10; B (25 > 20), C (30 = 30) not


def test_records_with_no_matching_record_in_a_linked_table(store):
    db, schema = store
    rows = built(db, schema, "offers not in variants", "t_offer",
                 {"r1_all": 0.02, "r1_has_t_variants": ("has_not", {"has_not": 0.96})}, {})
    assert rows == [["E", 1.0], ["F", 2.0]]                   # the NULL sku in variants does not defeat NOT IN
