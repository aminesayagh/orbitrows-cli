import saved_cases
from pathlib import Path

import pytest

from orbitrows.pipeline.store.profile import profile
from orbitrows.pipeline.store.roles import KIND_ROLE, classify
from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import Model, Recorded, settle

CASES = Path(__file__).parent / "fixtures" / "cases"
TABLES = [(f.parent.name, name, saved) for f in sorted(CASES.glob("*/result.json"))
          for name, saved in saved_cases.tables(f.parent.name).items()
          if not saved["roles"]["warnings"] and not any(c.get("flagged") for c in saved["roles"]["columns"].values())]

PICKED = [x for x in TABLES if x[0] in ("shop", "joins_chain")][:2]


class Replay(Model):
    """Answers each question with the kind and level saved for its column, sure."""

    def __init__(self, saved_columns):
        self.saved = saved_columns

    async def answer(self, state, questions):
        out = {}
        for k, q in questions.items():
            saved = self.saved[list(state["columns"]).index(k)]     # by the column's position
            pick = saved["level"] if "individual_record" in q["criteria"] else saved["kind"]
            out[k] = (pick, {pick: 0.95})
        return out, (None, None)


@pytest.mark.parametrize("case, name, saved", PICKED, ids=[f"{c}/{n}" for c, n, _ in PICKED])
async def test_sure_answers_give_the_saved_roles(case, name, saved):
    table = read_table(CASES / case / name, has_header=saved["read"]["has_header"])
    replay = Replay({h: c for h, c in saved["roles"]["columns"].items()})
    out = await classify(replay, table, saved["context"], saved["schema"], profile(table))
    for c in out["columns"].values():
        c.pop("probability")                                   # the PoC saved no probabilities
    assert out == saved["roles"]


class Stub(Model):
    """One answers dict per call (kinds, then key levels); records the questions asked."""

    def __init__(self, *calls):
        self.calls, self.asked = list(calls), []

    async def answer(self, state, questions):
        self.asked.append(set(questions))
        return self.calls.pop(0), (None, None)


def sure(option, p=0.9):
    return option, {option: p}


TABLE = {"name": "t", "header": ["sku", "label", "currency"], "rows": [["A1", "lamp", "EUR"], ["B2", "desk", "USD"]]}
SCHEMA = {"create_table": {"definition": {"columns": [{"header": h, "sql_type": "TEXT"} for h in TABLE["header"]]}}}
CONTEXT = {"context_columns": {2: {"context": "dedicated_context", "kind": "currency", "describes": []}},
           "source_context": {}}


def test_kind_role():
    assert {k: KIND_ROLE[k] for k in ("record_reference", "model_reference", "name", "attribute")} == \
        {"record_reference": "dedicated_key", "model_reference": "dedicated_key", "name": "descriptive_base",
         "attribute": "qualifier"}


async def test_kind_gives_the_role_and_a_context_column_is_not_asked():
    jev = Stub({"k1": sure("record_reference"), "k2": sure("name")}, {"k1": sure("individual_record")})
    out = await classify(jev, TABLE, CONTEXT, SCHEMA, profile(TABLE))
    assert jev.asked == [{"k1", "k2"}, {"k1"}]                  # no question for k3 (currency); levels for keys only
    assert out["columns"] == {0: {"role": "dedicated_key", "kind": "record_reference", "level": "individual_record",
                                  "probability": {"kind": 0.9, "level": 0.9}},
                              1: {"role": "descriptive_base", "kind": "name", "probability": {"kind": 0.9}}}
    assert out["warnings"] == []


async def test_uncertain_kind_keeps_the_likeliest_role_flagged_and_asks_the_user():
    calls = {}
    jev = Stub({"k1": ("attribute", {"attribute": 0.6, "name": 0.4}), "k2": sure("name")})
    out = await classify(Recorded(jev, calls), TABLE, CONTEXT, SCHEMA, profile(TABLE))
    assert out["columns"][0] == {"role": "qualifier", "kind": "attribute", "flagged": True,
                                 "probability": {"kind": 0.6}}
    [w] = out["warnings"]
    q = w.pop("question")
    assert w == {"column": "sku", "position": 0, "problem": "kind uncertain", "answer": "attribute", "probability": 0.6}
    assert q == {"type": "choice", "call": q["call"], "key": "k1",
                 "options": [{"answer": "attribute", "value": "attribute", "probability": 0.6},
                             {"answer": "name", "value": "name", "probability": 0.4}]}
    settle(calls, q["call"], "k1", "name")                     # the user: it is a name
    out = await classify(Recorded(Stub(), calls), TABLE, CONTEXT, SCHEMA, profile(TABLE))   # replayed, no call
    assert out["columns"][0] == {"role": "descriptive_base", "kind": "name", "probability": {"kind": 1.0}}
    assert out["warnings"] == []


async def test_uncertain_level_keeps_the_likeliest_flagged():
    jev = Stub({"k1": sure("record_reference"), "k2": sure("name")},
               {"k1": ("product_family", {"product_family": 0.55, "individual_record": 0.45})})
    out = await classify(jev, TABLE, CONTEXT, SCHEMA, profile(TABLE))
    assert out["columns"][0] == {"role": "dedicated_key", "kind": "record_reference", "level": "product_family",
                                     "flagged": True, "probability": {"kind": 0.9, "level": 0.55}}
    assert out["warnings"][0]["problem"] == "level uncertain"
