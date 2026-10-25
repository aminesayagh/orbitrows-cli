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
    columns = [{"expression": col(table, pk), "alias": "id"}]
    if entry["role"] == "condition":
        scope = {"type": "filtered", "where": node}
    else:
        columns.append({"expression": node, "alias": "v"})
        scope = {"type": "all_rows"}
    sql = program([{"id": "q", "type": "select", "from": {"type": "database_table", "reference": f"t_{table}",
                                                          "alias": f"t_{table}"}, "columns": columns, "scope": scope}],
                  store_of(db))[0]
    rounded = lambda rows: sorted(tuple(round(x, 6) if isinstance(x, float) else x for x in r) for r in rows)
    got, expected = rounded(db.execute(sql)), rounded(db.execute(want))
    assert got == expected and (got or entry_id == "WITHIN_LAST_DAYS"), (sql, got, expected)   # the last 30 days:
    # empty once the demo's sales are older (both sides compute the same date)
