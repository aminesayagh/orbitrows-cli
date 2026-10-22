import json
from pathlib import Path

import pytest

import saved_cases
from orbitrows.pipeline.store.schema import assemble, groups, round_trip
from orbitrows.services.csv.reader import read_table
from orbitrows.sql.ddl import NewTable, create_table, ddl

CASES = Path(__file__).parent / "fixtures" / "cases"
EXPECTED = json.loads((CASES / "store_schema_expected.json").read_text())


def summary(store) -> dict:
    """{table: {"pk": [columns], "refs": {column: table}}}, by SQL names: what the expected file states."""
    out = {}
    tables = {t["definition"]["table_id"]: t["definition"] for t in store["tables"]}
    for d in tables.values():
        n = {c["column_id"]: c["sql_name"] for c in d["columns"]}
        out[d["sql_name"]] = {"pk": [n[k] for k in d["primary_key"]],
                              "refs": {n[fk["columns"][0]]: tables[fk["references"]["table"]]["sql_name"]
                                       if fk["references"]["table"] in tables else fk["references"]["table"]
                                       for fk in d["foreign_keys"]}}
    return out


POC_NAMES = {"joins_chain": ["order", "customer"]}             # the proof of concept's table names, as the user's


@pytest.mark.parametrize("case", ["shop", "joins_chain"])
def test_case_matches_expected_and_round_trips(case):
    folder = CASES / case
    saved = json.loads((folder / "result.json").read_text())
    files = {f: s for f, s in saved_cases.tables(case).items() if f != "incoming.csv"}   # the store: one per session
    store = assemble(files, {g["id"]: n for g, n in zip(groups(files), POC_NAMES.get(case, []))})
    assert summary(store) == EXPECTED[case]
    rows = {f: read_table(folder / f, has_header=s["read"]["has_header"])["rows"] for f, s in files.items()}
    diffs = round_trip(store, rows)
    assert [d for d in diffs if d["kind"] == "value"] == []
    assert {d["kind"] for d in diffs} <= {"number_format"}
    old = saved["store_schema"]                               # what the proof-of-concept saved
    nodes = lambda ts: [{k: v for k, v in t.items() if k != "query"} for t in ts]   # its AST node had a query field (not ported)
    assert store["tables"] == nodes(old["tables"])
    assert store["metadata"] == [{k: v for k, v in m.items() if k != "edited_by"} for m in old["metadata"]]   # the PoC kept it in the rows
    assert store["export"] == old["export"]
    assert store["problems"] == old["problems"]


def sections(name, cols, groups=(), pk=None):
    node = create_table(name, [{"header": h, "sql_type": t, "nullable": True} for h, t in cols]).model_dump()
    if pk:
        node["definition"]["primary_key"] = [f"c_{pk}"]
    p = [h for h, _ in cols].index                            # the steps name a column by its position
    return {"read": {"header": [h for h, _ in cols], "has_header": True}, "schema": {"create_table": node},
            "roles": {"columns": {}}, "context": {"context_columns": {}, "source_context": {}},
            "joins": {"groups": [{"key": p(g["key"]), "columns": [p(c) for c in g["columns"]]} for g in groups]}}


COLS = [("order_id", "INTEGER"), ("customer_id", "INTEGER"), ("customer_name", "TEXT"), ("product_sku", "TEXT"),
        ("product", "TEXT"), ("qty", "INTEGER"), ("price", "REAL"), ("id", "TEXT")]
FLAT = sections("export", COLS, [{"key": "order_id", "columns": ["customer_id"]},
                                 {"key": "customer_id", "columns": ["customer_name"]},
                                 {"key": "product_sku", "columns": ["product"]}])
ROWS = [["1", "7", "Ana", "A-1", "Mug", "2", "12.5", "x"], ["1", "7", "Ana", "B-2", "Pan", "1", "40.0", "y"],
        ["2", "8", "Bo", "A-1", "Mug", "1", "12.5", ""], ["", "", "", "B-2", "Pan", "3", "40.0", "z"]]
STORE = assemble({"export.csv": FLAT})


def test_groups_become_tables_with_references():
    t = {x["definition"]["sql_name"]: x["definition"] for x in STORE["tables"]}
    assert list(t) == ["export", "record_1", "record_2", "record_3"], list(t)    # generated: no name from the key
    assert t["export"]["primary_key"] == ["c_row_id"] and t["export"]["columns"][0]["sql_name"] == "row_id"
    assert {fk["columns"][0]: fk["references"]["table"] for fk in t["export"]["foreign_keys"]} == {
        "c_order_id": "t_record_1", "c_product_sku": "t_record_3"}
    assert t["record_1"]["foreign_keys"][0]["references"]["table"] == "t_record_2" and STORE["problems"] == []
    assert [c["via"] for c in STORE["export"][0]["columns"]][:3] == [[], ["c_order_id"], ["c_order_id", "c_customer_id"]]


def test_round_trip_of_a_joined_export_is_lossless():
    assert round_trip(STORE, {"export.csv": ROWS}) == []


def test_a_group_value_that_disagrees_with_its_key_is_not_lost_silently():
    bad = [r[:] for r in ROWS]
    bad[2][4] = "Mug v2"
    assert round_trip(STORE, {"export.csv": bad}) == [
        {"file": "export.csv", "row": 2, "header": "product", "uploaded": "Mug v2", "rebuilt": "Mug", "kind": "value"}]


def test_a_real_cell_written_with_a_trailing_zero_is_a_number_format():
    got = round_trip(STORE, {"export.csv": [["1", "7", "Ana", "A-1", "Mug", "2", "12.50", ""]]})
    assert got[0]["kind"] == "number_format"


def test_lost_float_precision_is_a_value_difference():
    for lost in ("12345678901234567890", "3.14159265358979323846"):   # stored as 1.2345678901234567e+19, 3.141592653589793
        got = round_trip(STORE, {"export.csv": [["1", "7", "Ana", "A-1", "Mug", "2", lost, ""]]})
        assert got[0]["kind"] == "value" and got[0]["uploaded"] == lost, got


def test_a_leading_zero_is_a_value_difference():
    lead = round_trip(STORE, {"export.csv": [["1", "7", "Ana", "A-1", "Mug", "2", "012.50", ""]]})
    assert lead[0]["kind"] == "value" and lead[0]["uploaded"] == "012.50", lead


def test_a_reference_sqlite_cannot_resolve_is_reported():
    two = {"customers.csv": sections("customers", [("customer_id", "INTEGER"), ("name", "TEXT")], pk="customer_id"),
           "orders.csv": sections("orders", [("order_id", "INTEGER"), ("customer_id", "INTEGER")], pk="order_id")}
    two["orders.csv"]["schema"]["create_table"]["definition"]["foreign_keys"] = [
        {"columns": ["c_customer_id"], "references": {"table": "t_customers", "columns": ["c_customer_id"]}}]
    st = assemble(two)
    defs = {t["definition"]["table_id"]: NewTable(**t["definition"]) for t in st["tables"]}
    assert "ON DELETE RESTRICT ON UPDATE CASCADE" in ddl(defs["t_orders"], defs)
    got = round_trip(st, {"customers.csv": [["1", "Ana"]], "orders.csv": [["10", "1"], ["11", "2"]]})
    assert got == [{"table": "orders", "rowid": 11, "problem": "references a missing customers record", "kind": "value"}], got


def test_a_table_with_its_own_key_gets_no_added_id():
    one = assemble({"p.csv": sections("p", [("sku", "TEXT"), ("name", "TEXT")], pk="sku")})
    assert one["tables"][0]["definition"]["primary_key"] == ["c_sku"] and len(one["metadata"]) == 2
