import json
from pathlib import Path

import pytest

import saved_cases
from orbitrows.pipeline.store.keys import NONE, classify
from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import Model, Recorded, settle
from orbitrows.sql.ddl import create_table

CASES = Path(__file__).parent / "fixtures" / "cases"
EXPECTED = json.loads((CASES / "keys_expected.json").read_text())


class Stub(Model):
    """One answers dict per call (primary keys, then references); records the state and questions."""

    def __init__(self, *calls):
        self.calls, self.states, self.asked = list(calls), [], []

    async def answer(self, state, questions):
        self.states.append(state)
        self.asked.append(questions)
        return self.calls.pop(0), (None, None)


def sure(option, p=0.9):
    return option, {option: p}


def entry(name, cols, rows, roles, sql_types=None):
    """(table, saved sections) as the earlier steps leave them."""
    node = create_table(name, [{"header": h, "sql_type": (sql_types or {}).get(h, "INTEGER" if h.endswith("_id") else "TEXT"),
                                "nullable": True} for h in cols]).model_dump()
    table = {"name": name, "path": f"{name}.csv", "header": cols, "rows": rows}
    saved = {"schema": {"create_table": node}, "roles": {"columns": {cols.index(h): {"role": r} for h, r in roles.items()}},
             "profile": {"columns": {i: {"empty": 0, "distinct": len(rows)} for i in range(len(cols))}}}
    return table, saved


def customers(types=None):
    return entry("customers", ["customer_id", "name"], [["1", "Ana"], ["2", "Bo"]],
                 {"customer_id": "dedicated_key", "name": "none"}, types)


def orders(types=None):
    return entry("orders", ["order_id", "customer_id"], [["10", "1"], ["11", "2"], ["12", "2"]],
                 {"order_id": "dedicated_key", "customer_id": "dedicated_key"}, types)


async def test_table_without_dedicated_key_warns_and_asks_nothing():
    plain = entry("notes", ["text"], [["a"]], {"text": "none"})
    jev = Stub()
    got = await classify(jev, [plain])
    assert got["notes.csv"]["warnings"] == [{"column": None, "problem": "no dedicated_key column: no primary key"}]
    assert got["notes.csv"]["create_table"]["definition"]["primary_key"] == [] and jev.asked == []


async def test_reference_creates_a_foreign_key_with_the_right_column_ids():
    jev = Stub({"a": sure("a1"), "b": sure("b1")}, {"b2": sure("a1")})
    got = await classify(jev, [customers(), orders()])
    c, o = got["customers.csv"], got["orders.csv"]
    assert c["create_table"]["definition"]["primary_key"] == ["c_customer_id"]
    d = o["create_table"]["definition"]
    assert d["primary_key"] == ["c_order_id"]
    assert [(f["columns"], f["references"]) for f in d["foreign_keys"]] == [
        (["c_customer_id"], {"table": "t_customers", "columns": ["c_customer_id"]})]
    assert c["warnings"] == o["warnings"] == []
    assert set(jev.asked[1]) == {"b2"}                         # only the key column that is not orders' own key
    assert set(jev.states[1]["overlap"]["b2"]) == {"a1"} and jev.states[1]["overlap"]["b2"]["a1"] is not None


async def test_none_answer_makes_no_reference():
    jev = Stub({"a": sure("a1"), "b": sure("b1")}, {"b2": sure(NONE)})
    got = await classify(jev, [customers(), orders()])
    assert got["orders.csv"]["create_table"]["definition"]["foreign_keys"] == [] and got["orders.csv"]["warnings"] == []


async def test_uncertain_primary_key_is_kept_flagged_and_asked_by_header():
    calls = {}
    jev = Stub({"a": sure("a1"), "b": ("b1", {"b1": 0.6, "b2": 0.3, NONE: 0.1})}, {"b2": sure(NONE)})
    got = await classify(Recorded(jev, calls), [customers(), orders()])
    assert got["orders.csv"]["create_table"]["definition"]["primary_key"] == ["c_order_id"]
    [w] = got["orders.csv"]["warnings"]
    q = w.pop("question")
    assert w == {"column": None, "problem": "primary key uncertain", "answer": "b1", "probability": 0.6}
    assert q == {"type": "choice", "call": q["call"], "key": "b",
                 "options": [{"answer": "order_id", "value": "b1", "probability": 0.6},      # letters shown as headers
                             {"answer": "customer_id", "value": "b2", "probability": 0.3},
                             {"answer": NONE, "value": NONE, "probability": 0.1}]}
    settle(calls, q["call"], "b", "b2")                        # the user: customer_id
    replay = Stub({"b1": sure(NONE)})                          # the primary keys are replayed; order_id is now asked
    got = await classify(Recorded(replay, calls), [customers(), orders()])
    assert got["orders.csv"]["create_table"]["definition"]["primary_key"] == ["c_customer_id"]
    assert got["orders.csv"]["warnings"] == [] and [set(q) for q in replay.asked] == [{"b1"}]


async def test_uncertain_reference_is_kept_and_flagged():
    jev = Stub({"a": sure("a1"), "b": sure("b1")}, {"b2": ("a1", {"a1": 0.5, NONE: 0.5})})
    got = await classify(jev, [customers(), orders()])
    assert got["orders.csv"]["create_table"]["definition"]["foreign_keys"][0]["columns"] == ["c_customer_id"]
    [w] = got["orders.csv"]["warnings"]
    assert w | {"question": None} == {"column": "customer_id", "position": 1, "problem": "reference uncertain", "answer": "a1",
                                      "probability": 0.5, "question": None}
    assert w["question"]["key"] == "b2" and w["question"]["options"] == [
        {"answer": "customer_id (customers)", "value": "a1", "probability": 0.5},   # a column of another table
        {"answer": NONE, "value": NONE, "probability": 0.5}]


async def test_no_single_column_primary_key_warns():
    jev = Stub({"a": sure("a1"), "b": sure(NONE)}, {})
    got = await classify(jev, [customers(), orders()])
    assert got["orders.csv"]["create_table"]["definition"]["primary_key"] == []
    assert {"column": None, "problem": "no single-column primary key (composite: later)"} in got["orders.csv"]["warnings"]


async def test_reference_of_another_sqlite_type_is_reported_by_check_references():
    jev = Stub({"a": sure("a1"), "b": sure("b1")}, {"b2": sure("a1")})
    got = await classify(jev, [customers({"customer_id": "TEXT"}), orders()])
    problems = [w for w in got["orders.csv"]["warnings"] if w["column"] is None]
    assert len(problems) == 1 and "TEXT" in problems[0]["problem"] and "INTEGER" in problems[0]["problem"], problems
    assert got["customers.csv"]["warnings"] == []


class Replay(Model):
    """Answers (sure) the primary keys and references saved in the case's schema sections."""

    def __init__(self, saved):
        self.saved = {Path(f).stem: d for f, d in saved.items()}   # {file stem: schema create_table definition}

    async def answer(self, state, questions):
        files = {t: s["file"] for t, s in state["tables"].items()}
        out = {}
        for k, q in questions.items():
            ev = q["instructions"]
            if "table" in ev:                                  # the primary key of a table
                d = self.saved[files[k]]
                head = {c["column_id"]: c["header"] for c in d["columns"]}[d["primary_key"][0]] if d["primary_key"] else None
                pick = next((o for o in q["criteria"] if o != NONE and state["tables"][k]["columns"][o] == head), NONE)
            else:                                              # the reference of a column
                t, h = k[0], state["tables"][k[0]]["columns"][k]
                d = self.saved[files[t]]
                col = next(c["column_id"] for c in d["columns"] if c["header"] == h)
                fk = next((f for f in d["foreign_keys"] if f["columns"] == [col]), None)
                target = next((f for f, x in self.saved.items() if fk and x["table_id"] == fk["references"]["table"]), None)
                pick = next((o for o in q["criteria"] if o != NONE and files[o[0]] == target), NONE)
            out[k] = (pick, {pick: 0.95})
        return out, (None, None)


KEYED = [c for c in ("shop", "joins_chain") if all("keys" in s for s in json.loads((CASES / c / "result.json").read_text())["tables"].values())]


@pytest.mark.parametrize("case", KEYED)
async def test_sure_answers_give_the_saved_keys(case):
    saved = saved_cases.tables(case)
    tables = [(read_table(CASES / case / f, has_header=s["read"]["has_header"]), s) for f, s in sorted(saved.items())]
    got = await classify(Replay({f: s["schema"]["create_table"]["definition"] for f, s in saved.items()}), tables)
    for f, s in saved.items():
        want = s["schema"]["create_table"]["definition"]
        have = next(g for p, g in got.items() if Path(p).name == f)["create_table"]["definition"]
        refs = lambda d: [(f["columns"], f["references"]) for f in d["foreign_keys"]]   # the saved nodes predate on_delete
        assert (have["primary_key"], refs(have)) == (want["primary_key"], refs(want)), f
