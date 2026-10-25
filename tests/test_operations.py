"""The operations module (pipeline/operations): its catalog and its selection. No model loaded."""
import pytest

from fixtures.orbitrow.load import baseline
from orbitrows.pipeline.operations import catalog
from orbitrows.pipeline.operations.builders import BUILDERS
from orbitrows.pipeline.operations.inventory import inventory
from orbitrows.pipeline.prompt.pipeline import store_of
from orbitrows.sql.sql import program


def test_the_inventory_keeps_what_sqlite_runs_and_refuses_the_rest():
    found = {r["names"][0]: r for r in inventory()}
    runs = {n for n, r in found.items() if r["sqlite"]}
    assert {"ROUND", "COALESCE", "REPLACE", "SUBSTRING", "AVG", "LIKE", "BETWEEN"} <= runs
    assert {"RANK", "PERCENT_RANK", "LAG"} <= {n for n, r in found.items() if r["kind"] == "window"}
    assert not found["MEDIAN"]["sqlite"] and not found["STDDEV"]["sqlite"]     # not in SQLite: a composition, later



def col(t, c):
    return {"type": "column", "source": f"t_{t}", "reference": f"c_{c}"}


def lit(v):
    return {"type": "literal", "value": v}


PRICE, COST = col("variants", "price_mad"), col("variants", "cost_mad")
# entry → (its table, its resolved parameters, the SQL it must agree with on the demo store)
CHECKS = {
    "COMPARE_PERCENT_OF_COLUMN": ("variants", {"column": COST, "operator": "<", "percent": 40, "other": PRICE},
                                  "SELECT variant_id FROM variants WHERE cost_mad < price_mad * 0.4"),
    "COMPARE_MULTIPLE_OF_COLUMN": ("variants", {"column": PRICE, "operator": ">", "factor": 2, "other": COST},
                                   "SELECT variant_id FROM variants WHERE price_mad > 2 * cost_mad"),
    "COMPARE_SHARE_OF_FIRST": ("variants", {"column": PRICE, "operator": "<", "percent": 52, "other": COST},
                               "SELECT variant_id FROM variants WHERE (price_mad - cost_mad) * 100.0 / price_mad < 52"),
    "COMPARE_SHARE_OF_SECOND": ("variants", {"column": PRICE, "operator": ">", "percent": 110, "other": COST},
                                "SELECT variant_id FROM variants WHERE (price_mad - cost_mad) * 100.0 / cost_mad > 110"),
    "COMPARE_TABLE_AVERAGE": ("variants", {"column": PRICE, "operator": ">"},
                              "SELECT variant_id FROM variants WHERE price_mad > (SELECT AVG(price_mad) FROM variants)"),
    "COUNT_LINKED_RECORDS": ("products", {"column": col("products", "product_id"), "operator": ">", "number": 1,
                                          "linked": {"table": "t_variants", "key": "c_product_id"}},
                             "SELECT product_id FROM products p WHERE (SELECT COUNT(*) FROM variants v "
                             "WHERE v.product_id = p.product_id) > 1"),
    "WITHIN_LAST_DAYS": ("sales", {"column": col("sales", "sold_on"), "days": 30},
                         "SELECT sale_id FROM sales WHERE sold_on >= date('now', '-30 days')"),
    "INCREASE_PERCENT": ("variants", {"column": PRICE, "percent": 10},
                         "SELECT variant_id, price_mad * 1.1 FROM variants"),
    "CLAMP_MAX": ("variants", {"column": PRICE, "limit": 150}, "SELECT variant_id, MIN(price_mad, 150) FROM variants"),
    "SET_VALUE": ("variants", {"column": PRICE, "value": lit(99)}, "SELECT variant_id, 99 FROM variants"),
    "COMPARE_DIFFERENCE": ("variants", {"column": PRICE, "operator": ">=", "amount": 100, "other": COST},
                           "SELECT variant_id FROM variants WHERE price_mad - cost_mad >= 100"),
    "COMPARE_TABLE_MINIMUM": ("variants", {"column": PRICE, "operator": "="},
                              "SELECT variant_id FROM variants WHERE price_mad = (SELECT MIN(price_mad) FROM variants)"),
    "COMPARE_TABLE_MAXIMUM": ("variants", {"column": PRICE, "operator": "="},
                              "SELECT variant_id FROM variants WHERE price_mad = (SELECT MAX(price_mad) FROM variants)"),
    "SUM_LINKED_RECORDS": ("variants", {"column": col("variants", "variant_id"), "operator": "<", "number": 30,
                                        "linked": {"table": "t_inventory", "key": "c_variant_id",
                                                   "value": "c_quantity"}},
                           "SELECT variant_id FROM variants v WHERE (SELECT SUM(quantity) FROM inventory i "
                           "WHERE i.variant_id = v.variant_id) < 30"),
    "OLDER_THAN_DAYS": ("sales", {"column": col("sales", "sold_on"), "days": 40},
                        "SELECT sale_id FROM sales WHERE sold_on < date('now', '-40 days')"),
    "DECREASE_PERCENT": ("variants", {"column": PRICE, "percent": 15}, "SELECT variant_id, price_mad * 0.85 FROM variants"),
    "ADD_AMOUNT": ("variants", {"column": PRICE, "amount": 5}, "SELECT variant_id, price_mad + 5 FROM variants"),
    "SUBTRACT_AMOUNT": ("variants", {"column": PRICE, "amount": 5}, "SELECT variant_id, price_mad - 5 FROM variants"),
    "MULTIPLY_BY": ("variants", {"column": PRICE, "factor": 0.5}, "SELECT variant_id, price_mad * 0.5 FROM variants"),
    "SET_FROM_COLUMN": ("variants", {"column": PRICE, "other": COST, "amount": 20},
                        "SELECT variant_id, cost_mad + 20 FROM variants"),
    "ROUND_DECIMALS": ("variants", {"column": col("variants", "weight_g"), "digits": 0},
                       "SELECT variant_id, ROUND(weight_g) FROM variants"),
    "ROUND_TO_NEAREST": ("variants", {"column": PRICE, "step": 50}, "SELECT variant_id, ROUND(price_mad / 50.0) * 50 "
                                                                    "FROM variants"),
    "CLAMP_MIN": ("variants", {"column": PRICE, "limit": 100}, "SELECT variant_id, MAX(price_mad, 100) FROM variants"),
    "ADD_PREFIX": ("variants", {"column": col("variants", "sku"), "text": lit("NEW-")},
                   "SELECT variant_id, 'NEW-' || sku FROM variants"),
    "ADD_SUFFIX": ("variants", {"column": col("variants", "sku"), "text": lit("-FR")},
                   "SELECT variant_id, sku || '-FR' FROM variants"),
    "REPLACE_TEXT": ("products", {"column": col("products", "description"), "old": lit("cotton"),
                                  "new": lit("organic cotton")},
                     "SELECT product_id, REPLACE(description, 'cotton', 'organic cotton') FROM products"),
    "UPPER_CASE": ("products", {"column": col("products", "brand")}, "SELECT product_id, UPPER(brand) FROM products"),
    "LOWER_CASE": ("products", {"column": col("products", "brand")}, "SELECT product_id, LOWER(brand) FROM products"),
    "TRIM_SPACES": ("products", {"column": col("products", "description")},
                    "SELECT product_id, TRIM(description) FROM products"),
    "SHIFT_DATE": ("sales", {"column": col("sales", "sold_on"), "amount": -1, "unit": "months"},
                   "SELECT sale_id, DATE(sold_on, '-1 months') FROM sales"),
    "CLEAR_VALUE": ("variants", {"column": col("variants", "barcode")}, "SELECT variant_id, NULL FROM variants"),
    "SHOW_VALUES": ("variants", {"column": PRICE}, "SELECT price_mad FROM variants"),
    "COUNT_VALUES": ("variants", {"column": col("variants", "color")}, "SELECT COUNT(color) FROM variants"),
    "COUNT_DISTINCT_VALUES": ("variants", {"column": col("variants", "color")},
                              "SELECT COUNT(DISTINCT color) FROM variants"),
    "SUM_VALUES": ("variants", {"column": PRICE}, "SELECT SUM(price_mad) FROM variants"),
    "AVERAGE_VALUES": ("variants", {"column": PRICE}, "SELECT AVG(price_mad) FROM variants"),
    "MINIMUM_VALUE": ("variants", {"column": PRICE}, "SELECT MIN(price_mad) FROM variants"),
    "MAXIMUM_VALUE": ("variants", {"column": PRICE}, "SELECT MAX(price_mad) FROM variants"),
    "FILL_EMPTY": ("variants", {"column": col("variants", "color"), "value": lit("none"), "column_type": "TEXT"},
                   "SELECT variant_id, COALESCE(NULLIF(TRIM(color), ''), 'none') FROM variants"),
}


@pytest.fixture(scope="module")
def shipped():
    db = catalog.connect()                                    # the file the CLI ships, read-only
    yield db
    db.close()


def test_the_shipped_catalog_has_no_problem(shipped):
    assert catalog.problems(shipped) == []
    assert shipped.execute("SELECT COUNT(*) FROM operations WHERE role = 'technical' AND enabled = 0").fetchone()[0] > 500


def test_every_enabled_entry_is_checked_on_the_demo_store(shipped):
    assert {e["id"] for e in catalog.entries(shipped)} == set(CHECKS)


@pytest.mark.parametrize("entry_id", sorted(CHECKS))
def test_an_entry_builds_the_sql_it_describes(shipped, entry_id):
    entry, (table, p, want) = catalog.get(shipped, entry_id), CHECKS[entry_id]
    node = BUILDERS[entry["builder_key"]](p, entry["config"])
    db = baseline()
    pk = {"variants": "variant_id", "products": "product_id", "sales": "sale_id"}[table]
    columns = [] if entry["role"] == "output" else [{"expression": col(table, pk), "alias": "id"}]
    if entry["role"] == "output":                                    # what a read shows: the figure alone
        columns.append({"expression": node, "alias": "v"})
        scope = {"type": "all_rows"}
    elif entry["role"] == "condition":
        scope = {"type": "filtered", "where": node}
    else:
        columns.append({"expression": node, "alias": "v"})
        scope = {"type": "all_rows"}
    sql = program([{"id": "q", "type": "select", "from": {"type": "database_table", "reference": f"t_{table}",
                                                          "alias": f"t_{table}"}, "columns": columns, "scope": scope}],
                  store_of(db))[0]
    rounded = lambda rows: sorted(tuple(round(x, 6) if isinstance(x, float) else x for x in r) for r in rows)
    got, expected = rounded(db.execute(sql)), rounded(db.execute(want))
    assert got == expected and (got or entry_id in ("WITHIN_LAST_DAYS", "OLDER_THAN_DAYS")), (sql, got, expected)
    # a bound from today: empty, or every row, as the demo's dates age (both sides compute the same date)


def test_the_shipped_vectors_are_those_of_the_entries_as_they_are(shipped):
    """An entry changed without its vectors recomputed (index.embed) would be left out of the search: never ship it."""
    from orbitrows.pipeline.operations.index import Index
    index = Index(shipped)
    assert index.stale == [] and set(index.ids) == {e["id"] for e in catalog.entries(shipped)}


def test_a_changed_entry_is_left_out_of_the_search_until_embedded_again(tmp_path):
    import shutil
    from orbitrows.pipeline.operations.index import Index
    copy = tmp_path / "operations.db"
    shutil.copy(catalog.PATH, copy)
    db = catalog.connect(copy, write=True)
    entry = catalog.get(db, "CLAMP_MAX")
    catalog.save(db, {**entry, "examples": entry["examples"] + ["keep the scores at 100 at most"]})
    assert Index(db).stale == ["CLAMP_MAX"]                    # its old vectors are never matched
    db.close()


def test_the_selector_resolves_only_a_sure_choice_and_says_unsupported_only_when_sure():
    from orbitrows.pipeline.operations.selector import decided
    cands = ["CLAMP_MAX", "SET_VALUE"]
    assert decided(("CLAMP_MAX", {"CLAMP_MAX": 0.9}), cands)["status"] == "resolved"
    assert decided(("CLAMP_MAX", {"CLAMP_MAX": 0.6}), cands)["status"] == "needs_clarification"   # the user is asked
    assert decided(("none", {"none": 0.95}), cands)["status"] == "unsupported"                   # honest: no entry
    assert decided(("none", {"none": 0.6}), cands)["status"] == "needs_clarification"
    assert decided(None, cands)["status"] == "needs_clarification"                               # no answer (API)
