"""pipeline.run wires the steps as the PoC's run(): each step stubbed with a saved case's output, no model loaded."""
import json
from pathlib import Path

import pytest

from fixtures.orbitrow.load import baseline
from orbitrows.pipeline.prompt import (conditions, coref, formula, grouping, link, operation, parts, pipeline,
                                       request_graph, sources, values)
from orbitrows.pipeline.prompt.asking import question
from orbitrows.pipeline.store.profile import refresh
from orbitrows.pipeline.store.run import Paused
from orbitrows.services.model.client import Answers, Model, Recorded, ask, settle, yes_no

CASE = Path(__file__).parent / "fixtures" / "prompt_cases" / "A01.json"


async def test_run_returns_every_step_and_the_program(monkeypatch):
    saved = json.loads(CASE.read_text())["result"]["pipeline"]
    seen = []
    monkeypatch.setattr(pipeline.extractor, "connect", lambda: "gliner2")
    monkeypatch.setattr(coref, "mentions", lambda prompts: [[]])

    def stub(module, section):
        async def run(*args, **kwargs):
            seen.append((section, any(getattr(a, "inner", None) is model for a in args), "gliner2" in args))
            return saved[section]
        monkeypatch.setattr(module, "run", run)

    for module, section in ((request_graph, "requests"), (operation, "operation"), (values, "values"),
                            (sources, "sources"), (parts, "parts"), (conditions, "conditions"),
                            (grouping, "grouping"), (formula, "formula")):
        stub(module, section)
    steps = []
    model = Model()
    out = await pipeline.run(saved["prompt"], model, baseline(), steps.append)
    assert list(out) == ["prompt", "requests", "operation", "values", "sources", "parts", "conditions", "grouping",
                         "formula", "assembly"]
    assert json.loads(json.dumps(out["assembly"], default=str)) == saved["assembly"]
    assert [s for s, *_ in seen] == ["requests", "operation", "values", "sources", "parts", "conditions", "formula",
                                     "grouping"]                # formula first: its branches stay out of the tree
    assert all(m for _, m, _ in seen)                          # every step asks the caller's model
    assert [s for s, _, g in seen if g] == ["requests", "values", "conditions"]   # GLiNER2: steps 1, 3 and 6
    assert steps[-1] == "step 7 · assembly"


class Unsure(Model):
    """Answers every yes/no question 0.5: below every step's threshold."""

    def __init__(self):
        self.asked = []

    async def answer(self, state, questions):
        self.asked.append(list(questions))
        return {k: 0.5 for k in questions}, (None, None)


async def test_a_question_pauses_the_run_and_the_answer_lets_it_go_on(monkeypatch):
    saved = json.loads(CASE.read_text())["result"]["pipeline"]
    monkeypatch.setattr(pipeline.extractor, "connect", lambda: "gliner2")
    monkeypatch.setattr(coref, "mentions", lambda prompts: [[]])
    seen = []

    def stub(module, section, asks=False):
        async def run(model, *args, **kwargs):
            seen.append(section)
            out = json.loads(json.dumps(saved[section]))
            if asks:                                          # a step asking the model once, a notice beside
                a = await ask(model, {"step": section}, {"rows": yes_no("context", "Same rows?")})
                out["warnings"] = [{"problem": "a code rule"}] + (
                    [{"problem": "rows uncertain"} | question(a, "rows")] if 0.2 < a["rows"] < 0.8 else [])
            return out
        monkeypatch.setattr(module, "run", run)

    for module, section in ((request_graph, "requests"), (operation, "operation"), (values, "values"),
                            (sources, "sources"), (parts, "parts"), (conditions, "conditions"),
                            (grouping, "grouping"), (formula, "formula")):
        stub(module, section, asks=section == "values")
    inner, calls = Unsure(), {}
    with pytest.raises(Paused) as e:
        await pipeline.run(saved["prompt"], Recorded(inner, calls), baseline())
    p = e.value
    assert p.phase == "values" and seen == ["requests", "operation", "values"]   # stopped right after its step
    assert [(q["step"], q["question"]["type"], q["question"]["key"]) for q in p.questions] == [
        ("values", "yes_no", "rows")]
    assert p.questions[0]["id"].startswith("q_") and {"step": "values", "problem": "a code rule"} in p.notices
    settle(calls, p.questions[0]["question"]["call"], "rows", "yes")
    seen.clear()
    out = await pipeline.run(saved["prompt"], Recorded(inner, calls), baseline())   # replayed: no new call
    assert len(inner.asked) == 2 and seen[-1] == "grouping" and "assembly" in out   # the guard's and step 3's
    assert [w["problem"] for w in out["values"]["warnings"]] == ["a code rule"]   # the answer is certain: no question


class Statement(Model):
    """Step 2's model: a choice below SURE (0.7) for every statement, sure answers to the yes/no questions."""

    def __init__(self):
        self.asked = 0

    async def answer(self, state, questions):
        self.asked += 1
        return {k: ("update", {"update": 0.6, "select": 0.35, "insert": 0.05}) if k.endswith("_statement")
                else 0.0 for k in questions}, (None, None)


async def test_a_settled_answer_replays_as_certain():
    """A step's question (operation.py's), answered by the user: the replay asks nothing and takes the answer."""
    reqs, inner, calls = [{"id": "r1", "text": "show the red caps"}], Statement(), {}
    first = await operation.run(Recorded(inner, calls), "show the red caps", reqs, [])
    (w,) = first["warnings"]
    assert w["question"] == {"type": "choice", "call": next(iter(calls)), "key": "r1_statement", "options": [
        {"answer": "update", "value": "update", "probability": 0.6},
        {"answer": "select", "value": "select", "probability": 0.35}]}   # the near-zero option is not shown
    settle(calls, w["question"]["call"], "r1_statement", "select")
    again = await operation.run(Recorded(inner, calls), "show the red caps", reqs, [])
    assert inner.asked == 1 and again["requests"]["r1"]["statement"] == "select" and again["warnings"] == []


def test_step_1_carries_its_calls_to_the_questions_and_a_settled_answer_ends_them():
    """coref and link answer in their own calls, far from build_graph's warnings: the questions name those calls."""
    ms = [{"start": 0, "end": 4, "text": "them", "cluster": "1"}, {"start": 9, "end": 13, "text": "mugs", "cluster": "1"}]
    reqs = [{"text": "show them", "start": 0, "end": 4, "marker": None, "joined_by": None},
            {"text": "the mugs", "start": 5, "end": 13, "marker": None, "joined_by": None}]

    def graph(target, link_r1, input_r2):
        a = Answers({"m0_role": ("reference", {"reference": 0.9}), "m0_target": target,
                     "m1_role": ("not_reference", {"not_reference": 0.95}), "m1_target": ("created", {"created": 0.5})})
        a.call = "coref"
        answers = [{"choice": link_r1[0], "probabilities": link_r1[1]}, {"choice": "r1", "probabilities": {"r1": 0.9}}]
        linked = link.build(reqs, answers) | {"answers": answers, "inputs": {"r2": input_r2}, "call": "link"}
        return request_graph.build_graph(reqs, linked, coref.by_request(coref.decide(ms, a), reqs))

    unsure = graph(("m1", {"m1": 0.5, "ambiguous": 0.4}), ("none", {"none": 0.5, "r2": 0.5}),
                   ("rows", {"rows": 0.6, "table_state": 0.4}))
    assert {(w["problem"], w["question"]["call"], w["question"]["key"]) for w in unsure["warnings"]} == {
        ("unresolved reference", "coref", "m0_target"), ("uncertain link", "link", "r1"),
        ("input uncertain", "link", "r2_input")}
    assert [o["answer"] for w in unsure["warnings"] if w["problem"] == "unresolved reference"
            for o in w["question"]["options"]] == ["mugs"]   # named by its words; "ambiguous" is not offered
    settled = graph(("m1", {"m1": 1.0}), ("none", {"none": 1.0}), ("rows", {"rows": 1.0}))
    assert settled["warnings"] == []


def test_the_dry_runs_keep_the_session_s_transaction_open():
    """The session holds one open transaction (isolation_level None): SQLite's check must not commit it."""
    import sqlite3
    from orbitrows.sql import sql
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("CREATE TABLE t (x INTEGER)")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (1)")
    assert sql.check(db, ["UPDATE t SET x = 2"]) == [] and db.in_transaction
    db.execute("ROLLBACK")
    assert db.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)   # the insert was never committed


def ran_on(db, ran) -> list:
    """The rows a read of the program returns, run as the session runs it."""
    return db.execute(ran["sql"][0]).fetchall()


def offered(monkeypatch, statement, parts_, grouping_=None):
    """variants(sku, price) and an upload offer.csv(sku, recommended: text cells) linked on sku; one request r1 on
    the upload (its main table), every step stubbed. → the database."""
    import sqlite3
    from orbitrows.pipeline.prompt import source_route
    from orbitrows.services.storage import incoming, store
    db = sqlite3.connect(":memory:")
    db.executescript(store.SCHEMA + incoming.SCHEMA + """
        CREATE TABLE variants (sku TEXT PRIMARY KEY, price REAL);
        INSERT INTO variants VALUES ('A1', 100), ('B2', 120), ('C3', 50);
        INSERT INTO _history VALUES (1, 'now', 'import', NULL, '[]', '[]', NULL, 1);
        INSERT INTO _store_tables VALUES (1, 'variants', NULL, 'now', 1);
        INSERT INTO _store_columns (id, table_id, sql_name, sql_type, edited_by)
            VALUES (1, 1, 'sku', 'TEXT', 1), (2, 1, 'price', 'REAL', 1);
        INSERT INTO _incoming_tables VALUES (1, 'in_offer_1', 'incoming/1/offer.csv', 'now', 1);
        INSERT INTO _incoming_columns (id, table_id, sql_name, sql_type, position, extra_json)
            VALUES (1, 1, 'sku', 'TEXT', 0, '{"primary_key_candidate": true}'), (2, 1, 'recommended', 'TEXT', 1, '{}');
        CREATE TABLE in_offer_1 (sku TEXT, recommended TEXT);
        INSERT INTO in_offer_1 VALUES ('A1', '99.00'), ('B2', '129.00'), ('C3', '55.00');
        UPDATE _incoming_columns SET references_column_id = 1 WHERE id = 1;""")
    sections = {request_graph: {"requests": [{"id": "r1", "text": "m", "start": 0, "end": 1}], "references": [],
                                "warnings": []},
                operation: {"requests": {"r1": {"statement": statement}}, "warnings": []},
                values: {"values": [], "warnings": []},
                sources: {"requests": {"r1": {("from" if statement == "select" else "target"): {
                    "type": "database_table", "reference": "t_in_offer_1"}}}, "warnings": []},
                parts: {"requests": {"r1": parts_}, "warnings": []},
                conditions: {"requests": {}, "parameters": {}, "warnings": []},
                grouping: {"requests": {"r1": grouping_ or {}}, "warnings": []},
                formula: {"requests": {}, "warnings": []}}
    for module, section in sections.items():
        async def run(*a, section=section, **k):
            return json.loads(json.dumps(section))
        monkeypatch.setattr(module, "run", run)

    async def selected(model, prompt, db):
        return {"store": True, "incoming": source_route.available(db), "warnings": []}
    monkeypatch.setattr(source_route, "run", selected)
    monkeypatch.setattr(pipeline.extractor, "connect", lambda: "gliner2")
    monkeypatch.setattr(coref, "mentions", lambda prompts: [[]])
    refresh(db)                                               # the profile the upload counts
    return db


async def test_a_read_of_an_upload_shows_the_store_s_values_and_sorts_its_number_cells_as_numbers(monkeypatch):
    """The upload's key is linked one-to-one: its row reaches the store record (variants.price shown); its
    recommended cells are TEXT in the database, ordered as numbers (55, 99, 129), not as text."""
    out = lambda c: {"column": c, "aggregate": "value", "expression": {}}
    db = offered(monkeypatch, "select", {"outputs": [out("t_in_offer_1.c_sku"), out("t_variants.c_price"),
                                                     out("t_in_offer_1.c_recommended")]},
                 {"order_by": [{"expression": {"type": "column", "source": "t_in_offer_1",
                                               "reference": "c_recommended"}, "direction": "asc"}]})
    result = await pipeline.run("show the offer with our prices, by recommended price", Model(), db)
    assert result["assembly"]["errors"] == [], result["assembly"]
    [ran] = result["assembly"]["program"]
    assert ran_on(db, ran) == [("C3", 50.0, 55.0), ("A1", 100.0, 99.0), ("B2", 120.0, 129.0)], ran


async def test_a_write_to_an_upload_is_refused_as_read_only(monkeypatch):
    db = offered(monkeypatch, "update", {"assignments": [{"column": "t_in_offer_1.c_recommended",
                                                          "value": {"type": "literal", "value": "1"}}]})
    result = await pipeline.run("set the offer's recommended price to 1", Model(), db)
    [error] = result["assembly"]["errors"]
    assert error["error"] == "in_offer_1 is a read-only copy of incoming/1/offer.csv: it can't be changed.", error
    assert result["assembly"]["program"] == []
    assert db.execute("SELECT recommended FROM in_offer_1 ORDER BY sku").fetchall() == [
        ("99.00",), ("129.00",), ("55.00",)]


async def test_a_repeating_upload_key_is_linked_and_totalled_per_store_record(monkeypatch):
    """warehouse.csv has one row per sku and warehouse: its sku, a dedicated_key that repeats, is linked to
    variants.sku by its values (references_column_id), and a read of variants totals its qty per variant."""
    import sqlite3
    from orbitrows.pipeline import incoming_match
    from orbitrows.pipeline.prompt import source_route
    from orbitrows.services.storage import incoming, store
    table = {"header": ["sku", "warehouse", "qty"], "rows": [["A1", "Rabat", "4"], ["A1", "Fes", "6"],
                                                             ["B2", "Rabat", "1"], ["B2", "Fes", "2"]]}
    saved = {"schema": {"create_table": {"definition": {"columns": [{"column_id": f"c_{h}"} for h in table["header"]],
                                                        "primary_key": []}}},
             "context": {"context_columns": {}, "source_context": {}},
             "roles": {"columns": {0: {"role": "dedicated_key"}}},
             "profile": {"columns": {0: {"max_repeat": 2}, 1: {"max_repeat": 2}, 2: {"max_repeat": 1}}}}
    keys = [{"id": 1, "table": "variants", "header": "SKU", "sql_type": "TEXT", "values": ["A1", "B2", "C3"]}]
    linked = await incoming_match.link_keys(None, table, saved, {"tables": [], "keys": keys})
    assert linked == {"keys": {0: 1}, "warnings": []}
    db = sqlite3.connect(":memory:")
    db.executescript(store.SCHEMA + incoming.SCHEMA + """
        CREATE TABLE variants (sku TEXT PRIMARY KEY, stock INTEGER);
        INSERT INTO variants VALUES ('A1', 10), ('B2', 5);
        INSERT INTO _history VALUES (1, 'now', 'import', NULL, '[]', '[]', NULL, 1);
        INSERT INTO _store_tables VALUES (1, 'variants', NULL, 'now', 1);
        INSERT INTO _store_columns (id, table_id, sql_name, sql_type, edited_by)
            VALUES (1, 1, 'sku', 'TEXT', 1), (2, 1, 'stock', 'INTEGER', 1);
        INSERT INTO _incoming_tables VALUES (1, 'in_warehouse_1', 'incoming/1/warehouse.csv', 'now', 1);
        INSERT INTO _incoming_columns (id, table_id, sql_name, sql_type, position, extra_json)
            VALUES (1, 1, 'sku', 'TEXT', 0, '{"profile": {"max_repeat": 2}}'), (2, 1, 'warehouse', 'TEXT', 1, '{}'),
                   (3, 1, 'qty', 'TEXT', 2, '{}');
        CREATE TABLE in_warehouse_1 (sku TEXT, warehouse TEXT, qty TEXT);""")
    db.executemany("INSERT INTO in_warehouse_1 VALUES (?, ?, ?)", table["rows"])
    for pos, column in linked["keys"].items():                # as incoming.upload() writes it
        db.execute("UPDATE _incoming_columns SET references_column_id = ? WHERE position = ?", (column, pos))
    refresh(db)
    total = {"type": "aggregate", "function": "sum"}
    sections = {request_graph: {"requests": [{"id": "r1", "text": "m", "start": 0, "end": 1}], "references": [],
                                "warnings": []},
                operation: {"requests": {"r1": {"statement": "select"}}, "warnings": []},
                values: {"values": [], "warnings": []},
                sources: {"requests": {"r1": {"from": {"type": "database_table", "reference": "t_variants"}}},
                          "warnings": []},
                parts: {"requests": {"r1": {"outputs": [
                    {"column": "t_variants.c_sku", "aggregate": "value", "expression": {}},
                    {"column": "t_in_warehouse_1.c_qty", "aggregate": "sum", "expression": total}]}}, "warnings": []},
                conditions: {"requests": {}, "parameters": {}, "warnings": []},
                grouping: {"requests": {"r1": {"group_by": [{"source": "t_variants", "reference": "c_sku"}]}},
                           "warnings": []},
                formula: {"requests": {}, "warnings": []}}
    for module, section in sections.items():
        async def run(*a, section=section, **k):
            return json.loads(json.dumps(section))
        monkeypatch.setattr(module, "run", run)

    async def selected(model, prompt, db):
        return {"store": True, "incoming": source_route.available(db), "warnings": []}
    monkeypatch.setattr(source_route, "run", selected)
    monkeypatch.setattr(pipeline.extractor, "connect", lambda: "gliner2")
    monkeypatch.setattr(coref, "mentions", lambda prompts: [[]])
    result = await pipeline.run("total warehouse quantity per variant", Model(), db)
    assert result["assembly"]["errors"] == [], result["assembly"]
    [ran] = result["assembly"]["program"]
    assert sorted(ran_on(db, ran)) == [("A1", 10.0), ("B2", 3.0)], ran


def test_an_identifier_written_with_digits_stays_text_in_the_view():
    """A barcode's cells are all digits, but the roles step calls it a reference: a code to compare as text."""
    import sqlite3
    from orbitrows.services.storage import incoming
    from orbitrows.services.storage.store import KINDS
    db = sqlite3.connect(":memory:")
    db.executescript(incoming.SCHEMA.replace("REFERENCES _store_columns(id)", "") + f"""
        INSERT INTO _incoming_tables VALUES (1, 'in_offer_1', 'incoming/1/offer.csv', 'now', 1);
        INSERT INTO _incoming_columns (id, table_id, sql_name, sql_type, position, kind)
            VALUES (1, 1, 'barcode', 'TEXT', 0, {KINDS.index("record_reference")}), (2, 1, 'qty', 'TEXT', 1, NULL);
        CREATE TABLE in_offer_1 (barcode TEXT, qty TEXT);
        INSERT INTO in_offer_1 VALUES ('6110000000052', '3'), ('0610000000999', '12');""")
    refresh(db)
    assert pipeline.cell_type(db, "in_offer_1", "barcode") == "TEXT"
    assert pipeline.cell_type(db, "in_offer_1", "qty") == "INTEGER"


async def test_a_branch_of_the_formula_is_left_out_of_the_where_s_tree(monkeypatch):
    """"…but for the black ones…": color = 'black' is the CASE's branch, so grouping only joins the other conditions."""
    saved = json.loads(CASE.read_text())["result"]["pipeline"]
    monkeypatch.setattr(pipeline.extractor, "connect", lambda: "gliner2")
    monkeypatch.setattr(coref, "mentions", lambda prompts: [[]])
    black = {"type": "comparison", "operator": "=", "left": {"type": "column", "source": "t_variants",
                                                            "reference": "c_color"}, "right": {"type": "literal",
                                                                                               "value": "black"}}
    orbit = {"type": "comparison", "operator": "=", "left": {"type": "column", "source": "t_products",
                                                            "reference": "c_brand"}, "right": {"type": "literal",
                                                                                               "value": "Orbit"}}
    cond = {"requests": {"r1": {"columns": [], "predicates": [{"column": "t_variants.c_color", "condition": black},
                                                             {"column": "t_products.c_brand", "condition": orbit}]}},
            "warnings": []}
    joined = []
    for module, section in ((request_graph, "requests"), (operation, "operation"), (values, "values"),
                            (sources, "sources"), (parts, "parts")):
        monkeypatch.setattr(module, "run", lambda *a, section=section, **k: _section(saved[section]))
    monkeypatch.setattr(conditions, "run", lambda *a, **k: _section(cond))
    monkeypatch.setattr(formula, "run", lambda *a, **k: _section({"requests": {}, "warnings": [],
                                                                    "branches": {"r1": [black]}}))

    async def tree(model, prompt, reqs, store, sources_, c, params):
        joined.append([x["condition"] for x in c["requests"]["r1"]["predicates"]])
        return saved["grouping"]
    monkeypatch.setattr(grouping, "run", tree)
    await pipeline.run(saved["prompt"], Model(), baseline())
    assert joined == [[orbit]]                                 # black is the CASE's, not the WHERE's


async def _section(section):
    return json.loads(json.dumps(section))


async def test_the_guard_routes_a_sure_non_data_message_and_lets_a_doubt_run():
    from orbitrows.pipeline.prompt import guard

    class Says(Model):
        def __init__(self, kind, p):
            self.kind, self.p = kind, p

        async def answer(self, state, questions):
            return {k: (self.kind, {self.kind: self.p}) for k in questions}, (None, None)
    assert "`orbitrows undo`" in await guard.run(Says("undo", 0.95), "undo the last change")
    assert await guard.run(Says("undo", 0.6), "set the price back to 250") is None      # a doubt: the steps decide
    assert await guard.run(Says("data", 0.95), "show the variants") is None
