import json
from pathlib import Path

import pytest

from orbitrows.pipeline.store.types import LISTED, check_store, classify, classify_incoming
from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import Model
from orbitrows.sql.ddl import create_table

CASES = Path(__file__).parent / "fixtures" / "cases"
TABLES = [(f.parent.name, name, saved) for f in sorted(CASES.glob("*/result.json"))
          for name, saved in json.loads(f.read_text())["tables"].items()]
PICKED = [x for x in TABLES if x[0] in ("shop", "joins_chain")][:2]
NO_CONTEXT = {"context_columns": {}, "source_context": {}}


class Stub(Model):
    def __init__(self, answers):
        self.answers = answers

    async def answer(self, state, questions):
        return self.answers, (None, None)


def node(t, nullable=True):
    return create_table("t", [{"header": "x", "sql_type": t, "nullable": nullable}]).model_dump()


@pytest.mark.parametrize("case, name, saved", PICKED, ids=[f"{c}/{n}" for c, n, _ in PICKED])
def test_check_store_matches_the_saved_warnings(case, name, saved):
    table = read_table(CASES / case / name, has_header=saved["read"]["has_header"])
    expected = [w for w in saved["schema"]["warnings"] if "rows" in w]       # the rest are Jev's "type uncertain"
    assert check_store(table, saved["schema"]["create_table"]) == expected


def test_check_store():
    rows = [[str(i)] for i in range(1, 60)]
    rows[38] = ["n/a"]                                          # file line 40 (header is line 1)
    w = check_store({"rows": rows}, node("INTEGER"))
    assert len(w) == 1 and w[0]["count"] == 1 and w[0]["rows"][0] == {"row": 40, "value": "n/a", "problem": "does not fit INTEGER"}, w
    assert check_store({"rows": [["35"], [" 7 "], [""]]}, node("INTEGER")) == []      # lossless text and empty are fine
    assert check_store({"rows": [["35 kg"]]}, node("INTEGER"))[0]["rows"][0]["value"] == "35 kg"
    w = check_store({"rows": [["1.5"], ["1,5"]]}, node("REAL"))
    assert len(w) == 1 and w[0]["rows"][0]["value"] == "1,5", w
    w = check_store({"rows": [["a"], [""], ["  "]]}, node("TEXT", nullable=False))
    assert w[0]["count"] == 2 and w[0]["rows"][0]["problem"].startswith("empty"), w
    w = check_store({"rows": [["x"]] * 50}, node("INTEGER"))
    assert w[0]["count"] == 50 and len(w[0]["rows"]) == LISTED
    two = create_table("t", [{"header": h, "sql_type": t, "nullable": True} for h, t in (("a", "TEXT"), ("b", "INTEGER"))])
    w = check_store({"rows": [["x", "1"], ["y", "500 g"]]}, two.model_dump())      # a bad column after a good one
    assert [x["column"] for x in w] == ["b"], w
    w = check_store({"rows": [["007"], ["7"], ["+5"], ["1e3"]]}, node("INTEGER"))      # accepted by SQLite, changed
    assert [r["value"] for r in w[0]["rows"]] == ["007", "+5", "1e3"] and w[0]["rows"][0]["problem"].startswith("written as text"), w
    w = check_store({"rows": [["12.50"], ["350"], ["-0.5"], ["012.50"], [".5"]]}, node("REAL"))
    assert [r["value"] for r in w[0]["rows"]] == ["012.50", ".5"], w


def test_classify_incoming():
    inc = classify_incoming({"name": "t", "header": ["a", "b"], "rows": []})
    assert [c["sql_type"] for c in inc["create_table"]["definition"]["columns"]] == ["TEXT", "TEXT"] and inc["warnings"] == []


TABLE = {"name": "t", "header": ["sku", "qty"], "rows": [["A1", "3"], ["B2", "x"]]}


async def test_sure_answers_are_kept_and_checked_against_the_rows():
    jev = Stub({"k1_type": ("TEXT", {"TEXT": 0.95}), "k2_type": ("INTEGER", {"INTEGER": 0.9}),
                "k1_nullable": 0.05, "k2_nullable": 0.9})
    out = await classify(jev, TABLE, NO_CONTEXT)
    cols = out["create_table"]["definition"]["columns"]
    assert [(c["sql_type"], c["nullable"]) for c in cols] == [("TEXT", False), ("INTEGER", True)]
    assert [(w["column"], w["rows"][0]["value"]) for w in out["warnings"]] == [("qty", "x")]    # check_store's row


async def test_uncertain_type_keeps_text_and_unsure_nullability_is_nullable():
    jev = Stub({"k1_type": ("INTEGER", {"INTEGER": 0.6, "TEXT": 0.4}), "k2_type": ("INTEGER", {"INTEGER": 0.9}),
                "k1_nullable": 0.5})                            # k2_nullable missing counts as 1.0
    out = await classify(jev, TABLE, NO_CONTEXT)
    cols = out["create_table"]["definition"]["columns"]
    assert [(c["sql_type"], c["nullable"]) for c in cols] == [("TEXT", True), ("INTEGER", True)]
    w = out["warnings"][0]
    assert w | {"question": None} == {"column": "sku", "problem": "type uncertain: TEXT kept", "answer": "INTEGER",
                                      "probability": 0.6, "question": None}
    assert w["question"] == {"type": "choice", "call": w["question"]["call"], "key": "k1_type",     # for the user
                             "options": [{"answer": "INTEGER", "value": "INTEGER", "probability": 0.6},
                                         {"answer": "TEXT", "value": "TEXT", "probability": 0.4}]}
    assert len(w["question"]["call"]) == 16 and all("question" not in w for w in out["warnings"][1:])
