"""Step 7 is code only: the PoC's end-to-end cases (v2/prompt_ast/cases/A01–A50) replayed from their saved step outputs
on a fresh Orbitrow store must give back the saved program."""
import json
from pathlib import Path

import pytest

from fixtures.orbitrow.load import baseline
from orbitrows.pipeline.prompt import assemble
from orbitrows.pipeline.prompt.pipeline import store_of

CASES = [Path(__file__).parent / "fixtures" / "prompt_cases" / "A01.json"]
STEPS = ("requests", "operation", "values", "sources", "parts", "conditions", "grouping", "formula")


def replay(saved: dict) -> dict:
    """assemble() as pipeline.run calls it, fed the saved steps instead of the model's answers."""
    db = baseline()
    s1, s2 = saved["requests"], saved["operation"]
    reqs = [r | {k: v for k, v in s2["requests"][r["id"]].items() if k in ("statement", "input")}
            for r in s1["requests"]]
    return assemble.assemble(db, store_of(db), reqs, s2["requests"], saved["sources"]["requests"],
                             saved["parts"]["requests"], saved["conditions"]["requests"], saved["grouping"]["requests"],
                             [(k, saved[k]["warnings"]) for k in STEPS], order=s1.get("order"))


def sql(assembly: dict) -> list[str]:
    return [q for item in assembly["program"] for q in item.get("sql", [])]


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_assembly_replays(path):
    saved = json.loads(path.read_text())["result"]["pipeline"]
    got = replay(saved)
    assert sql(got) == sql(saved["assembly"])
    assert json.loads(json.dumps(got, default=str)) == saved["assembly"]   # the ASTs, row counts, questions, errors too


def test_a_read_of_the_rows_a_change_made_takes_the_ids_its_returning_gives():
    """"Raise the black variants by 5%, then show those": r1 returns its ids, r2 reads them as the session runs it
    (:rows_r1), so a variant still counts once its price no longer matches."""
    db = baseline()
    col = {"type": "column", "source": "t_variants", "reference": "c_price_mad"}
    black = {"type": "comparison", "operator": "=", "left": {"type": "column", "source": "t_variants",
                                                            "reference": "c_color"}, "right": {"type": "literal", "value": "black"}}
    reqs = [{"id": "r1", "text": "raise", "statement": "update"}, {"id": "r2", "text": "show those", "statement": "select"}]
    out = assemble.assemble(
        db, store_of(db), reqs, {"r1": {"statement": "update"}, "r2": {"statement": "select"}},
        {"r1": {"target": {"type": "database_table", "reference": "t_variants"}},
         "r2": {"from": {"type": "database_table", "reference": "t_variants"}}},
        {"r1": {"assignments": [{"column": "t_variants.c_price_mad", "value": {
            "type": "arithmetic", "operator": "*", "left": col, "right": {"type": "literal", "value": 1.05}}}]},
         "r2": {"outputs": [{"column": "t_variants.c_price_mad", "aggregate": "value", "expression": col}]}},
        {"r1": {"predicates": [{"column": "t_variants.c_color", "condition": black}]}, "r2": {"rows_from": "r1"}},
        {"r1": {"where": black}, "r2": {"rows_from": "r1", "where": None}}, [])
    assert not out["errors"], out
    first, second = (p["sql"][0] for p in out["program"])
    assert first.endswith('RETURNING "variant_id"') and ":rows_r1" in second, (first, second)


def test_a_value_a_read_computes_is_shown_as_its_own_column():
    """"Show the price minus the cost": the expressions module's tree (stage 3) is one column of the read."""
    db = baseline()
    price = {"type": "column", "source": "t_variants", "reference": "c_price_mad"}
    cost = {"type": "column", "source": "t_variants", "reference": "c_cost_mad"}
    margin = {"type": "arithmetic", "operator": "-", "left": price, "right": cost}
    reqs = [{"id": "r1", "text": "show the price minus the cost", "statement": "select"}]
    out = assemble.assemble(
        db, store_of(db), reqs, {"r1": {"statement": "select"}},
        {"r1": {"from": {"type": "database_table", "reference": "t_variants"}}},
        {"r1": {"outputs": [{"column": "t_variants.c_price_mad", "aggregate": "computed", "expression": margin}]}},
        {"r1": {}}, {"r1": {"where": None}}, [])
    assert not out["errors"], out
    sql = out["program"][0]["sql"][0]
    assert "computed_price_mad" in sql and sorted(db.execute(sql).fetchall(), key=repr) == sorted(
        db.execute("SELECT price_mad - cost_mad FROM variants").fetchall(), key=repr), sql   # no cost: NULL


def test_a_read_ordered_by_a_value_it_computes_sorts_by_that_expression():
    """"The 3 variants with the largest price minus cost": grouping's order names the computed value (step 5's)."""
    db = baseline()
    price = {"type": "column", "source": "t_variants", "reference": "c_price_mad"}
    cost = {"type": "column", "source": "t_variants", "reference": "c_cost_mad"}
    margin = {"type": "arithmetic", "operator": "-", "left": price, "right": cost}
    reqs = [{"id": "r1", "text": "the 3 largest price minus cost", "statement": "select"}]
    out = assemble.assemble(
        db, store_of(db), reqs, {"r1": {"statement": "select"}},
        {"r1": {"from": {"type": "database_table", "reference": "t_variants"}}},
        {"r1": {"outputs": [{"column": "t_variants.c_price_mad", "aggregate": "computed", "expression": margin,
                             "sort": True}]}},
        {"r1": {}}, {"r1": {"where": None, "order_by": [{"computed": True, "direction": "desc"}], "limit": 3}}, [])
    assert not out["errors"], out
    sql = out["program"][0]["sql"][0]
    assert [r[0] for r in db.execute(sql)] == [r[0] for r in db.execute(
        "SELECT price_mad - cost_mad FROM variants ORDER BY price_mad - cost_mad DESC LIMIT 3")], sql


def delete_sales(where):
    db = baseline()
    reqs = [{"id": "r1", "text": "delete those sales", "statement": "delete"}]
    out = assemble.assemble(db, store_of(db), reqs, {"r1": {"statement": "delete"}},
                            {"r1": {"from": {"type": "database_table", "reference": "t_sales"}}},
                            {"r1": {}}, {"r1": {}}, {"r1": {"where": where}}, [])
    return db, out


def test_a_condition_that_cannot_be_placed_stops_its_request_never_widens_it():
    """C06 (2026-10-28): a condition dropped as "comparing two tables" left a delete of every sale."""
    two = {"type": "comparison", "operator": ">",
           "left": {"type": "column", "source": "t_sales", "reference": "c_quantity"},
           "right": {"type": "column", "source": "t_variants", "reference": "c_price_mad"}}
    _, out = delete_sales(two)
    assert out["program"] == [] and any("could not be placed" in q["problem"] for q in out["questions"]), out


def test_a_window_condition_deletes_the_records_its_ranking_leaves_out():
    """"Delete every sale but the most recent of each variant": the window, computed in a subquery, in the WHERE."""
    from orbitrows.pipeline.operations import builders
    sales = lambda c: {"type": "column", "source": "t_sales", "reference": c}
    latest = builders.window_filter({"column": sales("c_sold_on"), "key": sales("c_sale_id"),
                                     "within": sales("c_variant_id"), "direction": "desc"},
                                    {"function": "rank", "operator": "<="})
    db, out = delete_sales({"type": "not", "operand": latest})
    assert not out["errors"], out
    for q in out["program"][0]["sql"]:
        db.execute(q)
    assert [r[0] for r in db.execute("SELECT sale_id FROM sales ORDER BY sale_id")] == [2, 4, 5, 6, 7, 8, 9, 10, 11,
                                                                                         12, 13, 14]


def limited(text, answers):
    """The grouping step on given answers for a sorted, limited read of the variants, then assembly."""
    from orbitrows.pipeline.prompt import grouping
    db = baseline()
    reqs = [{"id": "r1", "text": text, "statement": "select"}]
    sure = lambda v: (v, {v: 1.0})
    g = grouping.build(reqs, {"requests": {"r1": {"sorted": True, "limited": True, "predicates": []}}}, {},
                       {f"r1|{k}": sure(v) for k, v in answers.items()}, {}, store_of(db))["requests"]
    out = assemble.assemble(db, store_of(db), reqs, {"r1": {"statement": "select"}},
                            {"r1": {"from": {"type": "database_table", "reference": "t_variants"}}},
                            {"r1": {"outputs": [{"column": "t_variants.c_variant_id", "aggregate": "value",
                                                 "expression": {"type": "column", "source": "t_variants",
                                                                "reference": "c_variant_id"}}]}}, {"r1": {}}, g, [])
    return db, out


def test_a_limit_in_each_group_keeps_the_first_records_of_every_group_never_a_limit_over_all():
    """"The cheapest variant of each product" (step 2.2): a window condition, its ties kept; "the second dearest"
    without groups is refused, never read as a limit."""
    db, out = limited("Show the cheapest variant of each product.",
                      {"sort": "t_variants.c_price_mad", "direction": "asc", "limit": "one", "per_group": "each",
                       "per": "t_variants.c_product_id"})
    sql = out["program"][0]["sql"][0]
    assert "LIMIT" not in sql and sorted(r[0] for r in db.execute(sql)) == sorted(r[0] for r in db.execute(
        "SELECT variant_id FROM variants v WHERE price_mad = (SELECT MIN(price_mad) FROM variants w "
        "WHERE w.product_id = v.product_id)")), sql
    _, out = limited("Show the second most expensive variant.",
                     {"sort": "t_variants.c_price_mad", "direction": "desc", "limit": "p0", "per_group": "all",
                      "per": "none"})
    assert out["program"] == [] and any("one place over all" in q["problem"] for q in out["questions"]), out


def test_the_ledger_refuses_a_request_whose_number_no_part_used():
    """A12 (2026-10-29): "without a barcode that weigh more than 200 grams" ran without the weight; "8 days" around its
    used 8 is one occurrence read twice, accounted for; two zeros are two pieces of evidence, never one value."""
    from orbitrows.pipeline.prompt import ledger
    r = {"id": "r1", "start": 0, "text": "the variants without a barcode that weigh more than 200 grams"}
    found = ledger.found(r, [], [{"text": "200 grams", "start": 51, "end": 60, "value": 200.0}], [])
    barcode_only = {"predicates": [{"column": "t_variants.c_barcode", "words": []}]}
    assert ledger.unaccounted(found, ledger.used(barcode_only, {}, [], {}, r["text"])) == ["the number '200 grams'"]
    with_weight = {"predicates": [{"column": "t_variants.c_weight_g", "words": [[51, 54]]}]}
    assert ledger.unaccounted(found, ledger.used(with_weight, {}, [], {}, r["text"])) == []
    twice = [("the number 'no'", 10, 12), ("the number 'nothing'", 23, 30)]
    assert ledger.unaccounted(twice, [(10, 12)]) == ["the number 'nothing'"]
