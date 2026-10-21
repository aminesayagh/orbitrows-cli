"""pipeline.run wires the steps as the PoC's run(): each step stubbed with a saved case's output, no model loaded."""
import json
from pathlib import Path

from fixtures.orbitrow.load import baseline
from orbitrows.pipeline.prompt import (conditions, coref, formula, grouping, operation, parts, pipeline, request_graph,
                                       sources, values)

CASE = Path(__file__).parent / "fixtures" / "prompt_cases" / "A01.json"


async def test_run_returns_every_step_and_the_program(monkeypatch):
    saved = json.loads(CASE.read_text())["result"]["pipeline"]
    seen = []
    monkeypatch.setattr(pipeline.extractor, "connect", lambda: "gliner2")
    monkeypatch.setattr(coref, "mentions", lambda prompts, name: [[]])

    def stub(module, section):
        async def run(*args, **kwargs):
            seen.append((section, "model" in args, "gliner2" in args))
            return saved[section]
        monkeypatch.setattr(module, "run", run)

    for module, section in ((request_graph, "requests"), (operation, "operation"), (values, "values"),
                            (sources, "sources"), (parts, "parts"), (conditions, "conditions"),
                            (grouping, "grouping"), (formula, "formula")):
        stub(module, section)
    steps = []
    out = await pipeline.run(saved["prompt"], "model", baseline(), steps.append)
    assert list(out) == ["prompt", "requests", "operation", "values", "sources", "parts", "conditions", "grouping",
                         "formula", "assembly"]
    assert json.loads(json.dumps(out["assembly"], default=str)) == saved["assembly"]
    assert [s for s, *_ in seen] == ["requests", "operation", "values", "sources", "parts", "conditions", "grouping",
                                     "formula"]
    assert all(m for _, m, _ in seen)                          # every step asks the caller's model
    assert [s for s, _, g in seen if g] == ["requests", "values", "conditions"]   # GLiNER2: steps 1, 3 and 6
    assert steps[-1] == "step 7 · assembly"
