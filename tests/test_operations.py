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
PRODUCT, VARIANT = col("variants", "product_id"), col("variants", "variant_id")
SEVENTH = {"type": "arithmetic", "operator": "/", "left": PRICE, "right": lit(7.0)}      # fractions
LOSS = {"type": "arithmetic", "operator": "/", "left": {"type": "arithmetic", "operator": "-", "left": COST,
                                                       "right": PRICE}, "right": lit(7.0)}   # negative fractions
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
    "SHIFT_DATE": ("sales", {"column": col("sales", "sold_on"), "amount": 7, "unit": "days"},
                   "SELECT sale_id, DATE(sold_on, '+7 days') FROM sales"),
    "SHIFT_DATE_EARLIER": ("sales", {"column": col("sales", "sold_on"), "amount": 1, "unit": "months"},
                           "SELECT sale_id, DATE(sold_on, '-1 months') FROM sales"),
    "REMOVE_TEXT": ("products", {"column": col("products", "name"), "old": lit(" Kids")},
                    "SELECT product_id, REPLACE(name, ' Kids', '') FROM products"),
    "CLEAR_VALUE": ("variants", {"column": col("variants", "barcode")}, "SELECT variant_id, NULL FROM variants"),
    "SHOW_VALUES": ("variants", {"column": PRICE}, "SELECT price_mad FROM variants"),
    "COUNT_VALUES": ("variants", {"column": col("variants", "color")}, "SELECT COUNT(color) FROM variants"),
    "COUNT_DISTINCT_VALUES": ("variants", {"column": col("variants", "color")},
                              "SELECT COUNT(DISTINCT color) FROM variants"),
    # step 2.2: over each product's variants, in the variants' order (ties never decide a value)
    "RANK_VALUES": ("variants", {"column": PRICE, "within": PRODUCT, "direction": "desc"},
                    "SELECT RANK() OVER (PARTITION BY product_id ORDER BY price_mad DESC) FROM variants"),
    "PERCENTILE_RANK": ("variants", {"column": PRICE, "within": PRODUCT, "direction": "asc"},
                        "SELECT PERCENT_RANK() OVER (PARTITION BY product_id ORDER BY price_mad) * 100 FROM variants"),
    "PREVIOUS_VALUE": ("variants", {"column": PRICE, "within": PRODUCT, "order": VARIANT, "direction": "asc"},
                       "SELECT LAG(price_mad) OVER (PARTITION BY product_id ORDER BY variant_id) FROM variants"),
    "NEXT_VALUE": ("variants", {"column": PRICE, "within": PRODUCT, "order": VARIANT, "direction": "asc"},
                   "SELECT LEAD(price_mad) OVER (PARTITION BY product_id ORDER BY variant_id) FROM variants"),
    "RUNNING_TOTAL": ("variants", {"column": PRICE, "order": VARIANT, "direction": "asc"},
                      "SELECT SUM(price_mad) OVER (ORDER BY variant_id) FROM variants"),
    "FIRST_IN_GROUP": ("variants", {"column": PRICE, "within": PRODUCT, "order": VARIANT, "direction": "asc"},
                       "SELECT FIRST_VALUE(price_mad) OVER (PARTITION BY product_id ORDER BY variant_id) FROM variants"),
    "LAST_IN_GROUP": ("variants", {"column": PRICE, "within": PRODUCT, "order": VARIANT, "direction": "asc"},
                      "SELECT LAST_VALUE(price_mad) OVER (PARTITION BY product_id ORDER BY variant_id ROWS BETWEEN "
                      "UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING) FROM variants"),
    "SUM_VALUES": ("variants", {"column": PRICE}, "SELECT SUM(price_mad) FROM variants"),
    "AVERAGE_VALUES": ("variants", {"column": PRICE}, "SELECT AVG(price_mad) FROM variants"),
    "MINIMUM_VALUE": ("variants", {"column": PRICE}, "SELECT MIN(price_mad) FROM variants"),
    "MAXIMUM_VALUE": ("variants", {"column": PRICE}, "SELECT MAX(price_mad) FROM variants"),
    "DIVIDE_BY": ("variants", {"column": PRICE, "divisor": 4}, "SELECT variant_id, price_mad / 4.0 FROM variants"),
    "CAPITALIZE": ("products", {"column": col("products", "description")},
                   "SELECT product_id, UPPER(SUBSTR(description, 1, 1)) || SUBSTR(description, 2) FROM products"),
    "ADD_PERCENT_OF_COLUMN": ("variants", {"column": PRICE, "percent": 10, "other": COST},
                              "SELECT variant_id, price_mad + cost_mad * 0.1 FROM variants"),
    "SUBTRACT_PERCENT_OF_COLUMN": ("variants", {"column": PRICE, "percent": 10, "other": COST},
                                   "SELECT variant_id, price_mad - cost_mad * 0.1 FROM variants"),
    "COMPARE_LINKED_AGGREGATE": ("products", {"column": col("products", "product_id"), "function": "count",
                                              "operator": ">", "number": 1,
                                              "linked": {"table": "t_variants", "key": "c_product_id"}},
                                 "SELECT product_id FROM products p WHERE (SELECT COUNT(*) FROM variants v "
                                 "WHERE v.product_id = p.product_id) > 1"),
    "FILL_EMPTY": ("variants", {"column": col("variants", "color"), "value": lit("none"), "column_type": "TEXT"},
                   "SELECT variant_id, COALESCE(NULLIF(TRIM(color), ''), 'none') FROM variants"),
    # 2026-10-28: operations SQLite runs that no entry reached
    "COMPARE_TEXT_LENGTH": ("products", {"column": col("products", "name"), "operator": ">", "number": 12},
                            "SELECT product_id FROM products WHERE LENGTH(name) > 12"),
    "COMPARE_ABSOLUTE_DIFFERENCE": ("variants", {"column": COST, "operator": "<", "amount": 60, "other": PRICE},
                                    "SELECT variant_id FROM variants WHERE ABS(cost_mad - price_mad) < 60"),
    "COMPARE_DAYS_BETWEEN": ("sales", {"column": col("sales", "sold_on"), "operator": ">", "number": 10,
                                       "other": lit("2026-09-15")},          # the demo store has no second date
                             "SELECT sale_id FROM sales WHERE JULIANDAY(sold_on) - JULIANDAY('2026-09-15') > 10"),
    "MULTIPLE_OF_NUMBER": ("variants", {"column": col("variants", "variant_id"), "divisor": 3},
                           "SELECT variant_id FROM variants WHERE variant_id % 3 = 0"),
    "EVEN_VALUES": ("variants", {"column": col("variants", "variant_id")},
                    "SELECT variant_id FROM variants WHERE variant_id % 2 = 0"),
    "ODD_VALUES": ("variants", {"column": col("variants", "variant_id")},
                   "SELECT variant_id FROM variants WHERE variant_id % 2 = 1"),
    "LIST_VALUES": ("variants", {"column": col("variants", "color")}, "SELECT GROUP_CONCAT(DISTINCT color) FROM variants"),
    "ROUND_UP": ("variants", {"column": SEVENTH}, "SELECT variant_id, CEIL(price_mad / 7.0) FROM variants"),
    "ROUND_DOWN": ("variants", {"column": LOSS}, "SELECT variant_id, FLOOR((cost_mad - price_mad) / 7.0) FROM variants"),
    "DROP_DECIMALS": ("variants", {"column": LOSS}, "SELECT variant_id, TRUNC((cost_mad - price_mad) / 7.0) FROM variants"),
    "ABSOLUTE_VALUE": ("variants", {"column": LOSS}, "SELECT variant_id, ABS((cost_mad - price_mad) / 7.0) FROM variants"),
    "SQUARE_ROOT": ("variants", {"column": col("variants", "weight_g")}, "SELECT variant_id, SQRT(weight_g) FROM variants"),
    "RAISE_TO_POWER": ("variants", {"column": col("variants", "weight_g"), "exponent": 2},
                       "SELECT variant_id, POWER(weight_g, 2) FROM variants"),
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
    rounded = lambda rows: sorted((tuple(round(x, 6) if isinstance(x, float) else x for x in r) for r in rows),
                                  key=repr)                         # NULL beside numbers (a first record's previous)
    got, expected = rounded(db.execute(sql)), rounded(db.execute(want))
    assert got == expected and got, (sql, got, expected)


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


def test_every_function_the_catalog_lets_the_ast_call_compiles_and_runs_on_sqlite():
    """Beyond the hand-mapped ones (sql/dml.py MAPPED), the AST calls what the inventory found SQLite runs, each at
    its minimum number of arguments (the form SQLite accepted, not SQLGlot's required fields)."""
    from orbitrows.sql import dml
    db = baseline()
    price = col("variants", "price_mad")
    assert {"ceil", "floor", "power", "sqrt"} <= set(dml.FUNCTIONS)
    for name in dml.FUNCTIONS:
        stmt = {"id": "q", "type": "select", "from": {"type": "database_table", "reference": "t_variants",
                                                     "alias": "t_variants"},
                "columns": [{"expression": {"type": "function", "name": name,
                                            "arguments": [price] * dml.ARITY[name][0]}, "alias": "v"}],
                "scope": {"type": "all_rows"}}
        db.execute(program([stmt], store_of(db))[0]).fetchall()



@pytest.mark.parametrize("function,operator,number,linked,sql", [
    ("count_distinct", ">", 1, ("t_variants", "c_product_id", "c_color"), "COUNT(DISTINCT color) FROM variants v "
     "WHERE v.product_id = p.product_id) > 1"),
    ("min", "<", 100, ("t_variants", "c_product_id", "c_price_mad"), "MIN(price_mad) FROM variants v "
     "WHERE v.product_id = p.product_id) < 100"),
])
def test_the_linked_aggregate_builds_each_aggregate(function, operator, number, linked, sql):
    db = baseline()
    p = {"column": col("products", "product_id"), "function": function, "operator": operator, "number": number,
         "linked": dict(zip(("table", "key", "value"), linked))}
    node = BUILDERS["linked_aggregate"](p, {})
    q = program([{"id": "q", "type": "select", "from": {"type": "database_table", "reference": "t_products",
                                                        "alias": "t_products"},
                  "columns": [{"expression": col("products", "product_id"), "alias": "id"}],
                  "scope": {"type": "filtered", "where": node}}], store_of(db))[0]
    assert sorted(db.execute(q)) == sorted(db.execute(f"SELECT product_id FROM products p WHERE (SELECT {sql}"))


def test_a_total_over_no_linked_record_is_zero():
    """"Stock under 10": the variant with no stock line (115) is kept, its total counted as 0."""
    db = baseline()
    p = {"column": col("variants", "variant_id"), "function": "sum", "operator": "<", "number": 10,
         "linked": {"table": "t_inventory", "key": "c_variant_id", "value": "c_quantity"}}
    q = program([{"id": "q", "type": "select", "from": {"type": "database_table", "reference": "t_variants",
                                                        "alias": "t_variants"},
                  "columns": [{"expression": col("variants", "variant_id"), "alias": "id"}],
                  "scope": {"type": "filtered", "where": BUILDERS["linked_aggregate"](p, {})}}], store_of(db))[0]
    assert 115 in [r[0] for r in db.execute(q)]


def linked(function, where=None, operator=">", number=0):
    db = baseline()
    p = {"column": col("products", "product_id"), "function": function, "operator": operator, "number": number,
         "linked": {"table": "t_variants", "key": "c_product_id"}, **({"where": where} if where else {})}
    q = program([{"id": "q", "type": "select", "from": {"type": "database_table", "reference": "t_products",
                                                        "alias": "t_products"},
                  "columns": [{"expression": col("products", "product_id"), "alias": "id"}],
                  "scope": {"type": "filtered", "where": BUILDERS["linked_aggregate"](p, {})}}], store_of(db))[0]
    return sorted(r[0] for r in db.execute(q))


BLACK = {"type": "comparison", "operator": "=", "left": col("variants", "color"), "right": lit("black")}


def test_a_filter_on_the_linked_records_counts_only_those():
    assert linked("count", BLACK, ">", 1) == [1]                 # the Orbit Tee: two black variants


def test_all_linked_records_meet_the_filter_and_an_empty_value_breaks_it():
    """All black: the mouse and the cap; the cable's variants have no color: not black, never left out."""
    assert linked("all", BLACK) == [4, 7]


def test_a_linked_aggregate_two_links_away_goes_through_the_middle_table():
    """Products whose total stock is under 20: inventory → variants → products; a product with no stock counts 0."""
    db = baseline()
    p = {"column": col("products", "product_id"), "function": "sum", "operator": "<", "number": 20,
         "linked": {"table": "t_inventory", "key": "c_variant_id", "value": "c_quantity",
                    "through": {"table": "t_variants", "key": "c_product_id", "id": "c_variant_id"}}}
    q = program([{"id": "q", "type": "select", "from": {"type": "database_table", "reference": "t_products",
                                                        "alias": "t_products"},
                  "columns": [{"expression": col("products", "product_id"), "alias": "id"}],
                  "scope": {"type": "filtered", "where": BUILDERS["linked_aggregate"](p, {})}}], store_of(db))[0]
    assert sorted(r[0] for r in db.execute(q)) == [2, 4, 5, 6, 7, 9, 10, 11, 12]
