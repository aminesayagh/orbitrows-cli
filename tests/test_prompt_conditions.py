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
    db.execute("CREATE TABLE offer (sku TEXT PRIMARY KEY REFERENCES variants (sku), recommended INTEGER)")  # whole numbers
    db.executemany("INSERT INTO variants VALUES (?, ?)", [("A", 10), ("B", 20), ("C", 30), (None, 5)])
    db.executemany("INSERT INTO offer VALUES (?, ?)", [("A", 8), ("B", 25), ("C", 30), ("E", 1), ("F", 2)])
    schema = [n.model_dump() for n in schema_from_sqlite(db)]
    for n in schema:                                          # offer is an upload: its link is the view's (virtual),
        for fk in n["definition"]["foreign_keys"]:            # so "not in variants" is a real choice (E, F)
            fk["virtual"] = True
    return db, schema


def built(db, schema, text, main, stage_1, stage_2, numbers=(), tree=None) -> list:
    """Steps 6 and 7 for one read of `main` (every column shown) → the rows it returns."""
    other = {"t_variants": "t_offer", "t_offer": "t_variants"}[main]
    requests = [{"id": "r1", "text": text, "statement": "select"}]
    sources = {"r1": {"from": {"type": "database_table", "reference": main}, "reads": [other]}}
    params = [{"text": str(n), "value": n, "unit": None} for n in numbers]
    section = conditions.build(requests, schema, sources, [], stage_1)
    conditions.build_2(db, requests, schema, section["requests"], [], {"r1": params}, {}, stage_2, section)
    tree = grouping.build(requests, section, {}, tree or {})["requests"]
    out = assemble.assemble(db, schema, requests, {"r1": {"statement": "select"}}, sources, {"r1": {"outputs": "all"}},
                            section["requests"], tree)
    assert not out["errors"], out
    return sorted((list(r) for r in db.execute(out["program"][0]["sql"][0])), key=str)   # as the session runs it


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


def test_two_columns_compared_with_each_other_are_one_condition(store):
    db, schema = store              # both chosen, each compared with the other, read reversed from the price (the
    # real run's answers): the surer side, recommended < price, is kept once
    a, b = "r1|t_variants.c_price", "r1|t_offer.c_recommended"
    rows = built(db, schema, "variants whose offer recommended price is below the price", "t_variants",
                 {"r1_all": 0.02, "r1_chooses_t_variants.c_price": 0.97, "r1_chooses_t_offer.c_recommended": 0.97},
                 {f"{a}|op": ("<", {"<": 0.90}), f"{a}|upper": ("none", {"none": 0.95}), f"{a}|not": 0.02,
                  f"{a}|value": ("c:t_offer.c_recommended", {"c:t_offer.c_recommended": 0.95}),
                  f"{b}|op": ("<", {"<": 0.99}), f"{b}|upper": ("none", {"none": 0.95}), f"{b}|not": 0.02,
                  f"{b}|value": ("c:t_variants.c_price", {"c:t_variants.c_price": 0.95})})
    assert rows == [["A", 10.0]]                              # not [] (price < recommended AND recommended < price)


def test_records_with_no_matching_record_in_a_linked_table(store):
    db, schema = store
    rows = built(db, schema, "offers not in variants", "t_offer",
                 {"r1_all": 0.02, "r1_has_t_variants": ("has_not", {"has_not": 0.96})}, {})
    assert rows == [["E", 1.0], ["F", 2.0]]                   # the NULL sku in variants does not defeat NOT IN


def test_a_required_key_to_its_parent_is_never_asked_as_a_matching_record():
    """Every variant has its product (a declared, required foreign key): "a matching product?" is no choice to ask."""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE products (product_id INTEGER PRIMARY KEY, brand TEXT)")
    db.execute("CREATE TABLE variants (sku TEXT PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES products (product_id))")
    schema = [n.model_dump() for n in schema_from_sqlite(db)]
    assert "t_products" not in conditions.keyed_to(schema, "t_variants")
    assert "t_variants" in conditions.keyed_to(schema, "t_products")   # "products without variants" stays a choice


def test_the_tree_follows_from_each_pair_s_and_or():
    """(black or heavy) and (Orbit or archived): AND across the two groups, OR inside each → one tree; answers no tree
    fits (only 0-1, 1-2, 2-3 joined by AND) → none, AND kept and flagged."""
    ands = {(0, 2), (0, 3), (1, 2), (1, 3)}
    assert grouping.tree_of([0, 1, 2, 3], lambda i, j: (min(i, j), max(i, j)) in ands) == \
        ("all", [("any", [0, 1]), ("any", [2, 3])])
    assert grouping.tree_of([0, 1, 2, 3], lambda i, j: (min(i, j), max(i, j)) in {(0, 1), (1, 2), (2, 3)}) is None


def test_two_numbers_on_one_column_are_two_conditions(store):
    """"under 15 or over 25": one condition per number written (not one for the column), joined by grouping."""
    db, schema = store
    key = "r1|t_variants.c_price"
    rows = built(db, schema, "variants priced under 15 or over 25", "t_variants",
                 {"r1_all": 0.02, "r1_chooses_t_variants.c_price": 0.97},
                 {f"{key}|n0|op": ("<", {"<": 0.95}), f"{key}|n0|not": 0.02,
                  f"{key}|n1|op": (">", {">": 0.95}), f"{key}|n1|not": 0.02}, numbers=(15, 25),
                 tree={"r1|0|1": ("or", {"or": 0.95})})
    assert rows == [["A", 10.0], ["C", 30.0], [None, 5.0]], rows       # B (20) is between: kept out
