import json
from pathlib import Path

import pytest

from orbitrows.pipeline.store.joins import candidates, classify, repeats
from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import Model, Recorded, settle

CASES = Path(__file__).parent / "fixtures" / "cases"
EXPECTED = json.loads((CASES / "joins_expected.json").read_text())

HEADER = ["order_id", "line_no", "unit_price", "customer_id", "customer_name", "status", "sku", "name", "lot"]
ROLES = {"order_id": {"role": "dedicated_key"}, "customer_id": {"role": "dedicated_key"},
         "sku": {"role": "dedicated_key"}, "lot": {"role": "dedicated_key"},
         "unit_price": {"role": "none"}, "status": {"role": "none"}}


def prof(distinct):                                           # 30 rows, no empty cell
    det = {"order_id": ["customer_id", "customer_name", "status"], "customer_id": ["customer_name", "status"],
           "sku": ["unit_price", "name"], "lot": ["unit_price"]}
    return {"rows": 30, "columns": {h: {"empty": 0, "distinct": distinct.get(h, 30)} |
                                    ({"determines": det[h]} if h in det else {}) for h in HEADER}}


# the customer repeats on 5 rows only: status (a value) stays with the order; the product repeats on 8 rows:
# its price stays on the row (the name moves); a key with only a thin value column is no candidate
THIN = prof({"order_id": 10, "customer_id": 25, "sku": 22, "lot": 25})
# enough repeats: the nearest group takes status, the product takes its price
THICK = prof({"order_id": 10, "customer_id": 15, "sku": 12, "lot": 20})


def test_support_minimum_and_nearest_group():
    assert repeats(THIN, "order_id") == 20 and repeats(THIN, "sku") == 8
    assert candidates(HEADER, ROLES, THIN) == {"order_id": ["customer_id", "status"], "customer_id": ["customer_name"],
                                               "sku": ["name"]}
    assert candidates(HEADER, ROLES, THICK) == {"order_id": ["customer_id"], "customer_id": ["customer_name", "status"],
                                                "sku": ["unit_price", "name"], "lot": ["unit_price"]}


def test_repeats_counts_the_profiled_rows():
    assert repeats({"rows": 100, "profiled": 40, "columns": {"x": {"empty": 5, "distinct": 10}}}, "x") == 25


class Stub(Model):                                            # Jev answers, by question key
    def __init__(self, p):
        self.p, self.asked = p, {}

    async def answer(self, state, questions):
        self.asked = questions
        return {k: self.p.get(k, 0.0) for k in questions}, (None, None)


async def run(p, profile=THICK):
    jev = Stub(p)
    got = await classify(jev, {"name": "t", "header": HEADER, "rows": []}, {"roles": {"columns": ROLES},
                                                                            "profile": profile})
    return {g["key"]: g["columns"] for g in got["groups"]}, got["warnings"], jev.asked


K = {h: f"k{i}" for i, h in enumerate(HEADER, 1)}
YES = {f"{K[a]}_joined": 0.9 for a in ("order_id", "customer_id", "sku")} | {
    "k1_has_k4": 0.9, "k4_has_k5": 0.9, "k7_has_k8": 0.9}


async def test_link_to_a_confirmed_record_is_deduced():
    # the order's customer key: Jev hesitates (0.6), but customers are a confirmed record and every order has one
    # customer, so the link is deduced; the customer's name is still Jev's call
    groups, _, _ = await run(YES | {"k1_has_k4": 0.6})
    assert groups == {"order_id": ["customer_id"], "customer_id": ["customer_name"], "sku": ["name"]}, groups


async def test_no_deduction_when_the_record_is_not_confirmed():
    groups, warnings, _ = await run(YES | {"k1_has_k4": 0.6, "k4_joined": 0.5})
    assert groups == {"sku": ["name"]} and {"column": "customer_id", "problem": "describes order_id? uncertain: kept on the row",
                                            "probability": 0.6} in [{k: v for k, v in w.items() if k != "question"}
                                                                    for w in warnings]


async def test_no_deduction_with_too_few_repeats():
    few = prof({"order_id": 25, "customer_id": 15, "sku": 12, "lot": 20})
    groups, _, _ = await run(YES | {"k1_has_k4": 0.6}, few)
    assert "customer_id" not in groups.get("order_id", []), groups


async def test_an_unconfirmed_record_receives_nothing():
    groups, warnings, _ = await run(YES | {"k1_joined": 0.5})
    assert "order_id" not in groups and groups["customer_id"] == ["customer_name"]
    assert [w | {"question": None} for w in warnings] == [
        {"column": "order_id", "problem": "joined record uncertain: not split", "probability": 0.5, "question": None}]


@pytest.mark.parametrize("answer", ["yes", "no"])
async def test_an_unsure_record_is_a_yes_no_question(answer):
    calls, table, saved = {}, {"name": "t", "header": HEADER, "rows": []}, {"roles": {"columns": ROLES},
                                                                           "profile": THICK}
    got = await classify(Recorded(Stub(YES | {"k1_joined": 0.5}), calls), table, saved)
    [w] = got["warnings"]
    assert w["question"] == {"type": "yes_no", "call": w["question"]["call"], "key": "k1_joined"}
    settle(calls, w["question"]["call"], "k1_joined", answer)
    got = await classify(Recorded(replay := Stub({}), calls), table, saved)
    assert ("order_id" in {g["key"] for g in got["groups"]}) == (answer == "yes")
    assert got["warnings"] == [] and replay.asked == {}           # replayed, no call


async def test_an_unsure_column_is_a_yes_no_question():
    calls, table, saved = {}, {"name": "t", "header": HEADER, "rows": []}, {"roles": {"columns": ROLES},
                                                                           "profile": THICK}
    got = await classify(Recorded(Stub(YES | {"k7_has_k8": 0.6}), calls), table, saved)
    [w] = got["warnings"]
    assert w["question"] == {"type": "yes_no", "call": w["question"]["call"], "key": "k7_has_k8"}
    settle(calls, w["question"]["call"], "k7_has_k8", "yes")
    got = await classify(Recorded(Stub({}), calls), table, saved)
    assert {"key": "sku", "columns": ["name"]} in got["groups"] and got["warnings"] == []


async def test_no_candidate_means_no_question():
    jev = Stub({})
    got = await classify(jev, {"name": "t", "header": ["a"], "rows": []},
                         {"roles": {"columns": {}}, "profile": {"rows": 1, "columns": {"a": {"empty": 0, "distinct": 1}}}})
    assert got == {"groups": [], "warnings": []} and jev.asked == {}


class Replay(Model):
    """Answers yes (sure) to exactly the questions that the expected groups imply."""

    def __init__(self, groups):
        self.members = {(g["key"], c) for g in groups for c in g["columns"]}
        self.keys = {g["key"] for g in groups}

    async def answer(self, state, questions):
        cols, out = state["columns"], {}
        for k, q in questions.items():
            ev = q["instructions"]
            a = cols[ev["column"]]
            yes = (a in self.keys) if "candidate" not in ev else (a, cols[ev["candidate"]]) in self.members
            out[k] = 0.95 if yes else 0.0
        return out, (None, None)


@pytest.mark.parametrize("file", [f for f in sorted(EXPECTED) if f.split("/")[0] in ("shop", "joins_chain")][:2])
async def test_sure_answers_give_the_expected_groups(file):
    case, name = file.split("/")
    saved = json.loads((CASES / case / "result.json").read_text())["tables"][name]
    table = read_table(CASES / file, has_header=saved["read"]["has_header"])
    got = await classify(Replay(EXPECTED[file]), table, saved)
    assert {g["key"]: set(g["columns"]) for g in got["groups"]} == {g["key"]: set(g["columns"]) for g in EXPECTED[file]}
