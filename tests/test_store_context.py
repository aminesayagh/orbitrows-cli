import json
from pathlib import Path

import pytest

from orbitrows.pipeline.store import context
from orbitrows.pipeline.store.profile import profile
from orbitrows.services.model.client import Model

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


async def test_uncertain_role_keeps_the_likeliest_flagged():
    roles = ROLES | {"k3": ("dedicated_context", {"dedicated_context": 0.55, "none": 0.45})}
    out = await context.classify(Stub(roles, LINKS), TABLE, schema(), profile(TABLE))
    assert out["context_columns"]["currency"]["flagged"] is True
    assert out["context_columns"]["currency"]["context"] == "dedicated_context"
    assert out["warnings"] == [{"column": "currency", "problem": "context option uncertain",
                                "answer": "dedicated_context", "probability": 0.55}]


async def test_context_column_describing_nothing_is_flagged_for_the_merchant():
    links = {"k3_category": sure("currency"), "k3_describes_k1": 0.05, "k3_describes_k2": 0.1}
    out = await context.classify(Stub(ROLES, links), TABLE, schema(), profile(TABLE))
    assert out["context_columns"]["currency"]["describes"] == []
    assert out["warnings"] == [{"column": "currency", "problem": "context column describes no column: the merchant decides"}]


async def test_unsure_category_is_unknown():
    links = LINKS | {"k3_category": ("currency", {"currency": 0.5, "other": 0.5})}
    out = await context.classify(Stub(ROLES, links), TABLE, schema(), profile(TABLE))
    assert out["context_columns"]["currency"]["kind"] == "unknown"
    assert out["warnings"][0]["problem"] == "context category unsure"
