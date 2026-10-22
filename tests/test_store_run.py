import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

import saved_cases
from orbitrows.pipeline.store import run, schema
from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import Model, Recorded, options, settle
from orbitrows.services.storage import store as storage
from orbitrows.services.storage.export import export

CASES = Path(__file__).parent / "fixtures" / "cases"
pytestmark = pytest.mark.usefixtures("no_gliner")
SECTIONS = {"read", "schema", "profile", "context", "roles", "keys", "joins"}


class Generic(Model):
    """Answers every question the same way: a choice takes its first option (0.9), a yes/no is no (0.1)."""

    def __init__(self):
        self.calls = 0

    async def answer(self, state, questions):
        self.calls += 1
        return {k: (next(iter(q["criteria"])), {next(iter(q["criteria"])): 0.9}) if q["type"] == "choice" else 0.1
                for k, q in questions.items()}, (None, None)


async def test_classify_runs_every_step_on_every_file():
    paths = sorted((CASES / "french").glob("*.csv"))
    steps, jev = [], Generic()
    saved = await run.classify(jev, paths, step=steps.append)
    assert list(saved) == [p.name for p in paths] and len(paths) == 2
    for f, s in saved.items():
        assert set(s) == SECTIONS, f
        assert {"header", "has_header", "probability"} <= set(s["read"])
        assert all("warnings" in s[name] for name in run.STEPS)
    assert jev.calls > 0 and "keys between the files" in steps
    diffs = schema.round_trip(run.build(saved)["store"], run.rows(paths, saved))   # the chain goes through to the proof,
    assert any("datatype mismatch" in d.get("problem", "") for d in diffs)         # which catches the stub's INTEGER for text


class Unsure(Generic):
    """Generic, except the answers in `given` ({key: answer}) wherever that key is asked."""

    def __init__(self, given):
        super().__init__()
        self.given = given

    async def answer(self, state, questions):
        out, usage = await super().answer(state, questions)
        return out | {k: v for k, v in self.given.items() if k in questions}, usage


async def test_a_second_run_replays_every_answer_without_a_call():
    paths, calls = sorted((CASES / "french").glob("*.csv")), {}
    first, second = Generic(), Generic()
    async with Recorded(first, calls) as m:
        saved = await run.classify(m, paths)
    async with Recorded(second, calls) as m:
        assert await run.classify(m, paths) == saved
    assert first.calls == len(calls) > 0 and second.calls == 0


async def test_an_unsure_header_is_a_question_then_the_user_answer():
    path, calls = CASES / "store" / "store.csv", {}
    with pytest.raises(run.Paused) as paused:
        await run.classify(Recorded(Unsure({"header": 0.5}), calls), [path])
    [q] = paused.value.questions
    assert paused.value.phase == "header" and (q["file"], q["step"], q["question"]["type"]) == ("store.csv", "header", "yes_no")
    with pytest.raises(run.Paused) as again:                                    # the replay asks the same question
        await run.classify(Recorded(stub := Unsure({"header": 0.5}), calls), [path])
    assert again.value.questions[0]["id"] == q["id"] and q["id"].startswith("q_") and stub.calls == 0
    settle(calls, q["question"]["call"], q["question"]["key"], "no")
    saved = await run.classify(Recorded(Unsure({"header": 0.5}), calls), [path])
    assert saved["store.csv"]["read"]["has_header"] is False and "warning" not in saved["store.csv"]["read"]


async def test_an_unsure_type_is_a_question_and_the_rows_that_do_not_fit_are_notices():
    path, calls = CASES / "store" / "store.csv", {}
    probs = {"INTEGER": 0.5, "REAL": 0.4, "TEXT": 0.1}
    with pytest.raises(run.Paused) as paused:
        await run.classify(Recorded(Unsure({"k1_type": ("INTEGER", probs)}), calls), [path])
    [q] = paused.value.questions
    assert paused.value.phase == "types" and q["step"] == "schema" and q["question"]["key"] == "k1_type"
    assert q["question"]["options"] == options(probs) and len(options(probs)) == 3
    notices = paused.value.notices
    assert notices and all(n["step"] == "schema" and "question" not in n and n["problem"] for n in notices)
    settle(calls, q["question"]["call"], "k1_type", q["question"]["options"][1]["value"])
    saved = await run.classify(Recorded(Unsure({"k1_type": ("INTEGER", probs)}), calls), [path])
    assert saved["store.csv"]["schema"]["create_table"]["definition"]["columns"][0]["sql_type"] == "REAL"


def fixture(case):
    return {f: s for f, s in saved_cases.tables(case).items() if f != "incoming.csv"}


def test_build_collects_every_warning():
    saved = fixture("shop")
    saved["orders.csv"]["read"] |= {"warning": "header row uncertain: first row kept as header", "probability": 0.6}
    saved["orders.csv"]["schema"]["warnings"].append({"column": "total", "type": "REAL", "count": 1,
                                                      "rows": [{"row": 2, "value": "x", "problem": "does not fit REAL"}]})
    saved["orders.csv"]["roles"]["warnings"].append({"column": "status", "problem": "role uncertain", "answer": "none"})
    saved["customers.csv"]["joins"]["warnings"].append({"column": "city", "problem": "joined record uncertain: not split"})
    saved["products.csv"]["keys"]["warnings"].append({"column": None, "problem": "no single-column primary key"})
    built = run.build(saved)
    assert set(built) == {"store", "warnings"}
    got = {(w["file"], w["step"], w["problem"]) for w in built["warnings"]}
    assert got >= {("orders.csv", "header", "header row uncertain: first row kept as header"),
                   ("orders.csv", "schema", "1 values do not fit REAL: does not fit REAL"),
                   ("orders.csv", "roles", "role uncertain"),
                   ("customers.csv", "joins", "joined record uncertain: not split"),
                   ("products.csv", "keys", "no single-column primary key")}
    assert next(w for w in built["warnings"] if w["problem"] == "role uncertain")["answer"] == "none"   # nothing dropped


def test_build_reports_the_schema_problems():
    saved = fixture("shop")
    columns = saved["orders.csv"]["schema"]["create_table"]["definition"]["columns"]
    next(c for c in columns if c["column_id"] == "c_customer_id")["sql_type"] = "TEXT"   # references an INTEGER key
    built = run.build(saved)
    assert {"file": "orders.csv", "step": "schema", "table_id": "t_orders",
            "problem": "TYPE_MISMATCH c_customer_id TEXT → t_customers.c_customer_id INTEGER"} in built["warnings"]


async def test_a_pause_holds_the_questions_of_one_call_per_step(tmp_path):
    path, calls = tmp_path / "t.csv", {}
    path.write_text("sku,price,currency\nA1,9.90,EUR\nB2,12.00,USD\n")
    given = {"header": 0.95, "k3_type": ("TEXT", {"TEXT": 0.95}),
             "k3": ("dedicated_context", {"dedicated_context": 0.55, "none": 0.45}),          # call 1: the roles
             "k3_category": ("currency", {"currency": 0.5, "other": 0.4})}                   # call 2: depends on k3
    with pytest.raises(run.Paused) as paused:
        await run.classify(Recorded(Unsure(given), calls), [path])
    [q] = paused.value.questions                                # not the category: k3's answer may make it moot
    assert paused.value.phase == "context" and q["question"]["key"] == "k3"
    settle(calls, q["question"]["call"], "k3", "dedicated_context")
    with pytest.raises(run.Paused) as again:                    # the replay asks the category, recorded: no call
        await run.classify(Recorded(stub := Unsure(given), calls), [path])
    [q] = again.value.questions
    assert again.value.phase == "context" and q["question"]["key"] == "k3_category" and stub.calls == 0


async def test_the_roles_step_is_told_which_other_file_key_holds_a_column_values(tmp_path):
    orders, lines = tmp_path / "orders.csv", tmp_path / "order_lines.csv"
    orders.write_text("order_id,customer\nO-1,Ann\nO-2,Bob\nO-3,Cy\n")
    lines.write_text("line_id,order_id,qty\nL-1,O-1,2\nL-2,O-1,5\nL-3,O-2,1\nL-4,O-3,7\n")

    async def found(paths):                                     # each file's roles profile, as Jev reads it
        seen, jev = {}, Unsure({"header": 0.95})
        answer = jev.answer

        async def capture(state, questions):
            if any("model_reference" in q.get("criteria", {}) for q in questions.values()):
                seen[state["file"]] = {state["columns"][k]: c["found_in"]
                                       for k, c in state["profile"]["columns"].items() if "found_in" in c}
            return await answer(state, questions)
        jev.answer = capture
        try:
            await run.classify(jev, paths)
        except run.Paused:
            pass
        return seen

    assert await found([orders, lines]) == {
        "orders": {}, "order_lines": {"order_id": [{"file": "orders.csv", "column": "order_id", "share": 1.0, "covers": 1.0}]}}
    assert await found([orders]) == {"orders": {}}             # one file: nothing to be found in


async def test_two_columns_with_the_same_header_stay_two_columns(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    path = tmp_path / "prices.csv"
    path.write_text("id,Price,Price\n1,10,12\n2,20,25\n3,30,12\n")
    saved = await run.classify(Unsure({"header": 0.95}), [path])
    storage.copy_files("st_twin", [path])
    storage.write("st_twin", run.build(saved)["store"], run.rows([path], saved))     # no Lost: nothing merged
    with closing(sqlite3.connect(storage.folder("st_twin") / "session.db")) as db:
        twins = db.execute("SELECT id, position, profile_json FROM _store_columns WHERE source_header = 'Price' "
                           "ORDER BY position").fetchall()
        assert [p for _, p, _ in twins] == [1, 2] and twins[0][0] != twins[1][0]
        assert [json.loads(j)["values"] for *_, j in twins] == [["10", "20", "30"], ["12", "25"]]   # each its own
        [written] = export(db, storage.folder("st_twin"), tmp_path / "out")
    back, upload = read_table(written["path"], has_header=True), read_table(path, has_header=True)
    assert (back["header"], back["rows"]) == (upload["header"], upload["rows"])


async def test_a_table_split_out_of_a_file_is_a_naming_question(monkeypatch):
    path, calls = CASES / "joins_chain" / "export.csv", {}
    joined = saved_cases.tables("joins_chain")["export.csv"]["joins"]                # order ← customer, by position

    async def joins(jev, table, s):
        return json.loads(json.dumps(joined))
    monkeypatch.setattr(run.joins, "classify", joins)
    with pytest.raises(run.Paused) as paused:
        await run.classify(Recorded(Unsure({"header": 0.95}), calls), [path])
    order, customer = paused.value.questions
    assert paused.value.phase == "naming" and customer["step"] == "naming"
    assert [order["question"], customer["question"]] == [{"type": "name", "default": "record_1"},
                                                         {"type": "name", "default": "record_2"}]
    assert customer["problem"] == ("These columns are their own record: customer_id, customer_name, customer_city. "
                                   "Name this table, or skip.")
    names = {order["id"]: "record_1", customer["id"]: "customers"}                 # skipped, then named
    saved = await run.classify(Recorded(stub := Unsure({}), calls), [path], names=names)   # no question on replay
    assert stub.calls == 0
    tables = [t["definition"]["sql_name"] for t in run.build(saved, names)["store"]["tables"]]
    assert tables == ["export", "record_1", "customers"]
