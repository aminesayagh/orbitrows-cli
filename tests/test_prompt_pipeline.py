"""pipeline.run wires the steps as the PoC's run(): each step stubbed with a saved case's output, no model loaded."""
import json
from pathlib import Path

import pytest

from fixtures.orbitrow.load import baseline
from orbitrows.pipeline.prompt import (conditions, coref, formula, grouping, link, operation, parts, pipeline,
                                       request_graph, sources, values)
from orbitrows.pipeline.prompt.asking import question
from orbitrows.pipeline.store.run import Paused
from orbitrows.services.model.client import Answers, Model, Recorded, ask, settle, yes_no

CASE = Path(__file__).parent / "fixtures" / "prompt_cases" / "A01.json"


async def test_run_returns_every_step_and_the_program(monkeypatch):
    saved = json.loads(CASE.read_text())["result"]["pipeline"]
    seen = []
    monkeypatch.setattr(pipeline.extractor, "connect", lambda: "gliner2")
    monkeypatch.setattr(coref, "mentions", lambda prompts, name: [[]])

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
                         "formula", "assembly", "matched"]
    assert json.loads(json.dumps(out["assembly"], default=str)) == saved["assembly"]
    assert [s for s, *_ in seen] == ["requests", "operation", "values", "sources", "parts", "conditions", "grouping",
                                     "formula"]
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
    monkeypatch.setattr(coref, "mentions", lambda prompts, name: [[]])
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
    assert len(inner.asked) == 1 and seen[-1] == "formula" and "assembly" in out
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
    """The session holds one open transaction (isolation_level None): the pipeline's dry runs must not commit it."""
    import sqlite3
    from orbitrows.pipeline.prompt import assemble
    from orbitrows.sql import sql
    db = sqlite3.connect(":memory:", isolation_level=None)
    db.execute("CREATE TABLE t (x INTEGER)")
    db.execute("BEGIN")
    db.execute("INSERT INTO t VALUES (1)")
    assert sql.check(db, ["UPDATE t SET x = 2"]) == [] and db.in_transaction
    assert assemble.execute(db, [], [], []) == [] and db.in_transaction
    db.execute("ROLLBACK")
    assert db.execute("SELECT COUNT(*) FROM t").fetchone() == (0,)   # the insert was never committed
