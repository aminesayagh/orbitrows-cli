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


def test_one_occurrence_is_one_condition_s_and_a_word_of_none_belongs_to_its_own_column():
    """B25 (2026-10-29): "sold on or after October 4" read the 4 of the date again as quantity = 4: refused. A word of
    none is an empty test's evidence only when the parse attaches it to that column's words ("no barcode")."""
    from orbitrows.pipeline.prompt import conditions, ledger
    text = "sales with a quantity of exactly 2, sold on or after October 4"
    d = text.find("October 4")
    two = {"predicates": [{"words": [[d, d + 9]], "condition": {"date": ">="}},
                          {"words": [[d + 8, d + 9]], "condition": {"quantity": "="}}]}
    assert ledger.shared(two, text) == ["'4'"] and ledger.shared({"predicates": [{"words": [[d, d + 9]]}]}, text) == []
    same = {"type": "comparison"}                             # the same condition built twice: a duplicate
    assert ledger.shared({"predicates": [{"words": [[d, d + 9]], "condition": same},
                                         {"words": [[d, d + 9]], "condition": same}]}, text) == []
    twice = "created in September and sold in September"      # the second September, used by no condition
    assert ledger.shared({"predicates": [{"words": [[11, 20]], "condition": {"created": "in"}},
                                         {"words": [[11, 20]], "condition": {"sold": "in"}}]}, twice) == []
    r = {"id": "r1", "text": "products with no barcode and no stock"}
    params = [{"text": "no", "start": 14, "end": 16, "value": 0, "none": True},
              {"text": "no", "start": 29, "end": 31, "value": 0, "none": True}]
    named = {"r1": {"t_variants.c_barcode": {"words": ["barcode"]}}}
    assert conditions.none_words(r, "t_variants.c_barcode", params, named) == [[14, 16]]


def test_step_6_sees_a_quantity_of_none():
    from orbitrows.units.parameters import parameters

    class NoSpans:
        def extract_entities(self, *a, **k):
            return {"entities": {}}
    got = [(p["text"], p["value"]) for p in parameters("lines with no stock and nothing reserved", NoSpans())]
    assert got == [("no", 0), ("nothing", 0)]


def test_a_number_s_unit_names_its_column_and_a_condition_s_number_is_not_the_formula_s():
    """ "weigh more than 200 grams": no word names the weight, its unit does (stage 1 asks about it); "raise by 5% …
    that cost more than 60": the 60 is the condition's, read by step 6, not a step the formula lost."""
    from orbitrows.pipeline.expressions.links import unused_numbers
    from orbitrows.pipeline.prompt.conditions import asked_columns
    from orbitrows.pipeline.store.profile import refresh
    db = baseline()
    refresh(db)
    schema = store_of(db)
    r = {"id": "r1", "text": "Show the variants that weigh more than 200 grams.", "statement": "select"}
    grams = {"r1": [{"text": "200 grams", "value": 200.0, "unit": {"kind": "physical_unit", "unit": "g"}}]}
    sources = {"r1": {"from": {"type": "database_table", "reference": "t_variants"}}}
    asked = asked_columns(schema, [r], sources, "r1", {"r1": {}}, {}, db, grams)
    assert "t_variants.c_weight_g" in asked and "t_variants.c_price_mad" not in asked
    text = "Raise by 5% the price of the variants that cost more than 60."
    i = text.find("60")
    up = {"predicates": [{"text": "cost above 60", "words": [[i, i + 2]]}]}
    assert unused_numbers(text, up, [], {}) == ["the number '5%' is not used in the formula"]   # not the 60


def test_an_amount_s_none_is_zero_and_a_negation_the_request_does_not_mean_is_dropped():
    """ "with a discount": the discount is 0 when there is none, its NULL test alone was always true; "the October
    sales except the refunded ones": October negated too, asked again as built (2n) and kept."""
    from orbitrows.pipeline.prompt.conditions import build_2n, predicate
    col = {"type": "column", "source": "t_sales", "reference": "c_discount_mad"}
    assert predicate("is_not_empty", col, None, None, [], False, amount=True)["operator"] == "!="
    assert predicate("is_not_empty", col, None, None, [], False)["type"] == "null_check"   # a key: NULL only
    october = {"type": "comparison", "operator": "=", "left": col, "right": {"type": "literal", "value": 1}}
    kept = {"column": "t_sales.c_sold_on", "negated": True,
            "condition": {"type": "any", "children": [{"type": "null_check"}, {"type": "not", "operand": october}]}}
    section = {"requests": {"r1": {"predicates": [kept]}}}
    build_2n(section, [("r1|kept0", "r1", kept)], {"r1|kept0": 0.05})
    assert kept["condition"] == october and not kept["negated"]


def test_contested_words_go_to_one_condition_and_an_id_s_key_answers_add_up():
    """ "created in September … sold in September": each September on both dates; Jev, shown both, gives each its
    own (2x). "variant 101": sales.variant_id 0.55 and variants.variant_id 0.35 are one key, sure together."""
    from orbitrows.pipeline.prompt.conditions import build_2x
    from orbitrows.pipeline.prompt.values import same_key
    created, sold = ({"column": c, "mention": "d1", "words": [[70, 79]], "condition": {}} for c in ("p.created",
                                                                                                     "s.sold"))
    section = {"requests": {"r1": {"predicates": [created, sold]}}}
    build_2x(section, [("r1|owner70", "r1", {"0": created, "1": sold})], {"r1|owner70": ("1", {"1": 0.95})})
    assert section["requests"]["r1"]["predicates"] == [sold]
    schema = store_of(baseline())
    assert same_key(schema, {"t_sales.c_variant_id": 0.55, "t_variants.c_variant_id": 0.35}) == "t_sales.c_variant_id"
    assert same_key(schema, {"t_sales.c_variant_id": 0.55, "t_variants": 0.4}) is None


def test_an_or_joins_the_branches_of_the_sentence_s_or():
    """ "the September sales with a discount above 5 or a quantity of at least 2": the discount and the quantity are
    the two branches; September is in neither, so it is joined to them by AND."""
    from orbitrows.pipeline.prompt.grouping import apart
    t = "Show the September sales with a discount above 5 or a quantity of at least 2."
    at = lambda w: [[t.find(w), t.find(w) + len(w)]]
    assert apart(t, at("5"), at("2")) is True
    assert apart(t, at("September"), at("2")) is False and apart(t, at("September"), at("5")) is False
    assert apart(t, [], at("2")) is None



def test_a_negated_leaf_on_a_linked_table_is_negated_inside_its_chain():
    """ "sales of products from suppliers outside Morocco": NOT inside the IN chain (sales whose supplier's country
    is not Morocco), not around it (which kept the sales of products with no supplier)."""
    from orbitrows.pipeline.prompt.assemble import Names, lift
    schema = store_of(baseline())
    leaf = {"type": "comparison", "operator": "=", "left": {"type": "column", "source": "t_suppliers",
                                                             "reference": "c_country"},
            "right": {"type": "literal", "value": "Morocco"}}

    out = lift({"type": "not", "operand": leaf}, "t_sales", "s", schema, Names(schema), [], "r1")
    assert out["type"] != "not" and '"not"' in json.dumps(out)


def test_a_percentage_slot_takes_a_number_written_as_one_and_more_numbers_than_named_columns_asks_the_others():
    """Fresh set F23: "raise … by 20" ran price * 1.2; F15: "nothing in stock and nothing reserved" named only the
    reserved column, so the quantity's 0 was lost."""
    from orbitrows.pipeline.expressions.links import not_percentages
    from orbitrows.pipeline.prompt.conditions import asked_columns
    step = lambda t: [{"role": "calculation", "function": "entry:INCREASE_PERCENT", "start": 0, "end": len(t),
                       "right": {"slot": "percent", "value": 20}}]
    t = "Raise the price of the Hoodie variants by 20."
    assert not_percentages(t, step(t)) and not not_percentages(t.replace("20", "20%"), step(t + "%"))
    t = "Lower the price of the variants heavier than 20 g by 20%."   # the same number twice, one a percentage
    assert not not_percentages(t, step(t))
    schema = store_of(baseline())
    t = "Remove the inventory lines that have nothing in stock and nothing reserved."
    zeros = {"r1": [{"text": "nothing", "value": 0, "start": t.find("nothing"), "end": t.find("nothing") + 7},
                    {"text": "nothing", "value": 0, "start": t.rfind("nothing"), "end": t.rfind("nothing") + 7}]}
    r = {"id": "r1", "text": t, "statement": "delete"}
    sources = {"r1": {"target": {"type": "database_table", "reference": "t_inventory"}}}
    asked = asked_columns(schema, [r], sources, "r1", {"r1": {"t_inventory.c_reserved": {}}}, {}, None, zeros)
    assert "t_inventory.c_quantity" in asked and "t_inventory.c_warehouse" not in asked
    one = {"r1": zeros["r1"][1:]}                             # one number, one named column: nothing added
    assert "t_inventory.c_quantity" not in asked_columns(schema, [r], sources, "r1",
                                                         {"r1": {"t_inventory.c_reserved": {}}}, {}, None, one)


def test_a_column_named_in_full_and_not_a_unit_alone():
    """Hard set H10 ("counting each sale as its quantity times its unit price, minus its discount"): quantity and
    discount are named in full, so a group total on the unit price alone isn't the request's computation; the "mad"
    of cost_mad alone names nothing."""
    from orbitrows.pipeline.prompt.conditions import in_full
    schema = store_of(baseline())
    assert in_full(schema, "t_sales.c_quantity", {"how": "word", "words": ["quantity"]})
    assert in_full(schema, "t_sales.c_unit_price_mad", {"how": "word", "words": ["unit", "price", "mad"]})
    assert not in_full(schema, "t_variants.c_cost_mad", {"how": "word", "words": ["mad"]})
    assert not in_full(schema, "t_sales.c_quantity", {"how": "meaning", "words": ["units"]})


def test_a_column_jev_leans_against_is_left_out_unasked_and_one_it_leans_towards_is_asked():
    """Set G: "does variant_id choose the rows?" at yes 0.42 was asked, and no was right 5 times of 6; leaning no, the
    ledger catches a value only that column could take."""
    from orbitrows.pipeline.prompt.conditions import build
    from orbitrows.services.model.client import Answers
    schema = store_of(baseline())
    requests = [{"id": "r1", "statement": "select", "text": "Show the variants that have no barcode."}]
    sources = {"r1": {"from": {"type": "database_table", "reference": "t_variants"}}}

    def asked(p):
        answers = Answers({"r1_chooses_t_variants.c_sku": p})
        answers.call = "c1"
        return [w for w in build(requests, schema, sources, [], answers)["warnings"] if "question" in w]
    assert asked(0.42) == [] and len(asked(0.6)) == 1


def test_a_column_with_several_written_numbers_still_gets_its_blank_condition(no_gliner):
    """Composed F01 (2026-11-01): "without a cost" survives numbers used by the price formula."""
    from orbitrows.pipeline.prompt import conditions
    from orbitrows.pipeline.store.profile import refresh
    from orbitrows.services.model.client import Answers
    db = baseline()
    refresh(db)
    schema = store_of(db)
    key = "t_variants.c_cost_mad"
    text = "Raise the Orbit variants by 15%, then take 5 off. Leave the variants without a cost unchanged."
    r = {"id": "r1", "text": text, "statement": "update"}
    params = {"r1": [{"text": s, "value": v, "unit": None, "start": text.find(s), "end": text.find(s) + len(s)}
                     for s, v in (("15%", 15), ("5", 5))]}
    stage1 = {"r1": {"columns": [key]}}
    questions = conditions.questions_2(db, [r], schema, stage1, [], params, {"r1": {}})
    op_key = f"r1|{key}|blank|op"
    assert set(questions[op_key]["criteria"]) == {"is_empty", "is_not_empty", "none"}
    assert f"r1|{key}|blank|not" in questions

    def built(op):
        answers = Answers({f"r1|{key}|n{i}|op": ("none", {"none": 0.9}) for i in range(2)} |
                          {op_key: (op, {op: 0.9}), f"r1|{key}|blank|not": 0.9})
        section = {"requests": {"r1": {"columns": [key], "predicates": []}}, "warnings": [],
                   "named": {"r1": {key: {"words": ["cost"]}}}}
        conditions.build_2(db, [r], schema, stage1, [], params, {"r1": {}}, answers, section)
        return section["requests"]["r1"]["predicates"], section["warnings"]

    got, warnings = built("is_empty")
    assert not warnings and len(got) == 1
    assert got[0]["column"] == key and got[0]["operator"] == "is_empty" and got[0]["negated"]
    assert got[0]["condition"]["type"] == "not"
    assert got[0]["condition"]["operand"]["type"] in ("null_check", "empty_check", "any")
    assert "words" in got[0] and "at" in got[0]
    assert built("none")[0] == []
