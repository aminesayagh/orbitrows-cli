import json
from pathlib import Path

import pytest

from orbitrows.pipeline.store import context
from orbitrows.pipeline.store.profile import profile
from orbitrows.services.model.client import Model, Recorded, options, settle

TABLE = {"name": "t", "header": ["sku", "price", "currency"],
         "rows": [["A1", "9.90", "EUR"], ["B2", "12.00", "USD"]]}


@pytest.fixture(autouse=True)
def no_header_units(monkeypatch):
    """The header readers (GLiNER2 + dictionary) are tested in test_units; here no header declares a unit."""
    monkeypatch.setattr(context, "declaration", lambda header, kind=None: None)


class Stub(Model):
    """One answers dict per call (roles, then categories and links)."""

    def __init__(self, *calls):
        self.calls, self.asked = list(calls), []

    async def answer(self, state, questions):
        self.asked.append(set(questions))
        return self.calls.pop(0), (None, None)


def sure(option, p=0.95):
    return option, {option: p}


def schema(currency_type="TEXT"):
    types = {"sku": "TEXT", "price": "REAL", "currency": currency_type}
    return {"create_table": {"definition": {"columns": [{"header": h, "sql_type": t} for h, t in types.items()]}}}


ROLES = {"k1": sure("none"), "k2": sure("none"), "k3": sure("dedicated_context")}
LINKS = {"k3_category": sure("currency"), "k3_describes_k1": 0.05, "k3_describes_k2": 0.95}


async def test_dedicated_context_column_describes_the_column_it_is_linked_to():
    jev = Stub(ROLES, LINKS)
    out = await context.classify(jev, TABLE, schema(), profile(TABLE))
    assert out["context_columns"] == {"currency": {"context": "dedicated_context", "kind": "currency",
                                                   "describes": ["price"]}}
    assert out["source_context"] == {"price": {"kind": "currency", "from": "column", "column": "currency"}}
    assert out["warnings"] == [] and jev.asked[1] == {"k3_category", "k3_describes_k1", "k3_describes_k2"}


@pytest.mark.parametrize("numeric", ["INTEGER", "REAL"])
async def test_numeric_dedicated_context_is_demoted_with_a_warning(numeric):
    jev = Stub(ROLES, {})
    out = await context.classify(jev, TABLE, schema(numeric), profile(TABLE))
    assert out["context_columns"] == {} and out["source_context"] == {}
    assert out["warnings"] == [{"column": "currency", "problem": f"dedicated_context but typed {numeric}: none kept"}]
    assert len(jev.asked) == 1                                  # no second call: nothing left to link


async def test_uncertain_role_keeps_the_likeliest_flagged_and_asks_the_user():
    roles = ROLES | {"k3": ("dedicated_context", {"dedicated_context": 0.55, "none": 0.45})}
    calls = {}
    out = await context.classify(Recorded(Stub(roles, LINKS), calls), TABLE, schema(), profile(TABLE))
    assert out["context_columns"]["currency"]["flagged"] is True
    assert out["context_columns"]["currency"]["context"] == "dedicated_context"
    [w] = out["warnings"]
    q = w.pop("question")
    assert w == {"column": "currency", "problem": "context option uncertain", "answer": "dedicated_context",
                 "probability": 0.55}
    assert q == {"type": "choice", "call": q["call"], "key": "k3", "options": options(roles["k3"][1])}
    settle(calls, q["call"], q["key"], "none")                 # the user: not a context column
    out = await context.classify(Recorded(Stub(), calls), TABLE, schema(), profile(TABLE))   # replayed, no call
    assert out["context_columns"] == {} and out["warnings"] == []


async def test_a_sure_uncertain_role_is_a_notice():
    roles = ROLES | {"k3": sure("uncertain", 0.9)}
    out = await context.classify(Stub(roles, {}), TABLE, schema(), profile(TABLE))
    assert out["warnings"] == [{"column": "currency", "problem": "context option uncertain", "answer": "uncertain",
                                "probability": 0.9}]


@pytest.mark.parametrize("answer, describes", [("yes", ["sku", "price"]), ("no", ["price"])])
async def test_an_unsure_link_is_a_yes_no_question(answer, describes):
    links, calls = LINKS | {"k3_describes_k1": 0.4}, {}
    out = await context.classify(Recorded(Stub(ROLES, links), calls), TABLE, schema(), profile(TABLE))
    assert out["context_columns"]["currency"]["describes"] == ["price"]
    [w] = out["warnings"]
    assert w == {"column": "currency", "problem": "does it describe sku?", "probability": 0.4, "linked": False,
                 "question": {"type": "yes_no", "call": w["question"]["call"], "key": "k3_describes_k1"}}
    settle(calls, w["question"]["call"], "k3_describes_k1", answer)
    out = await context.classify(Recorded(Stub(), calls), TABLE, schema(), profile(TABLE))
    assert out["context_columns"]["currency"]["describes"] == describes and out["warnings"] == []


async def test_a_column_the_header_names_is_linked_without_a_question():
    table = {"name": "t", "header": ["sku", "price", "price currency"], "rows": [["A1", "9.90", "EUR"]]}
    types = {"sku": "TEXT", "price": "REAL", "price currency": "TEXT"}
    links = LINKS | {"k3_describes_k2": 0.5}                    # unsure, but the header names price: no answer could change it
    out = await context.classify(Stub(ROLES, links), table, {"create_table": {"definition": {"columns": [
        {"header": h, "sql_type": t} for h, t in types.items()]}}}, profile(table))
    assert out["context_columns"]["price currency"]["describes"] == ["price"] and out["warnings"] == []


async def test_an_unsure_header_letter_is_a_question(monkeypatch):
    table = {"name": "t", "header": ["sku", "Volume (L)"], "rows": [["A1", "0.5"], ["B2", "1.5"]]}
    litre = {"unit": "l", "kind": "physical_unit"}
    monkeypatch.setattr(context, "declaration", lambda header, kind=None: litre if header == "Volume (L)" else None)
    types = {"create_table": {"definition": {"columns": [{"header": "sku", "sql_type": "TEXT"},
                                                         {"header": "Volume (L)", "sql_type": "REAL"}]}}}
    calls = {}
    stub = Stub({"k1": sure("none"), "k2": sure("header_context")}, {"k2_letter": 0.5})
    out = await context.classify(Recorded(stub, calls), table, types, profile(table))
    [w] = out["warnings"]
    assert w["question"] == {"type": "yes_no", "call": w["question"]["call"], "key": "k2_letter"}
    assert out["source_context"] == {}
    settle(calls, w["question"]["call"], "k2_letter", "yes")
    out = await context.classify(Recorded(Stub(), calls), table, types, profile(table))
    assert out["source_context"] == {"Volume (L)": {"kind": "physical_unit", "from": "header", "unit": "l"}}
    assert out["warnings"] == []
    sure_no = Stub({"k1": sure("none"), "k2": sure("header_context")}, {"k2_letter": 0.1})
    out = await context.classify(sure_no, table, types, profile(table))
    assert "question" not in out["warnings"][0]                 # a sure no: a notice


async def test_context_column_describing_nothing_is_flagged_for_the_merchant():
    links = {"k3_category": sure("currency"), "k3_describes_k1": 0.05, "k3_describes_k2": 0.1}
    out = await context.classify(Stub(ROLES, links), TABLE, schema(), profile(TABLE))
    assert out["context_columns"]["currency"]["describes"] == []
    assert out["warnings"] == [{"column": "currency", "problem": "context column describes no column: the merchant decides"}]


async def test_unsure_category_is_unknown_and_a_question():
    links, calls = LINKS | {"k3_category": ("currency", {"currency": 0.5, "other": 0.5})}, {}
    out = await context.classify(Recorded(Stub(ROLES, links), calls), TABLE, schema(), profile(TABLE))
    assert out["context_columns"]["currency"]["kind"] == "unknown"
    [w] = out["warnings"]
    assert w["problem"] == "context category unsure" and w["question"] == {
        "type": "choice", "call": w["question"]["call"], "key": "k3_category",
        "options": [{"answer": "currency", "value": "currency", "probability": 0.5},
                    {"answer": "other", "value": "other", "probability": 0.5}]}
    settle(calls, w["question"]["call"], "k3_category", "other")
    out = await context.classify(Recorded(Stub(), calls), TABLE, schema(), profile(TABLE))
    assert out["context_columns"]["currency"]["kind"] == "other" and out["warnings"] == []
