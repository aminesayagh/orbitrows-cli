"""Step 7 is code only: the PoC's end-to-end cases (v2/prompt_ast/cases/A01–A50) replayed from their saved step outputs
on a fresh Orbitrow store must give back the saved program."""
import json
from pathlib import Path

import pytest

from fixtures.orbitrow.load import baseline
from orbitrows.pipeline.prompt import assemble
from orbitrows.pipeline.prompt.pipeline import store_of

CASES = sorted((Path(__file__).parent / "fixtures" / "prompt_cases").glob("A*.json"))
STEPS = ("requests", "operation", "values", "sources", "parts", "conditions", "grouping", "formula")


def replay(saved: dict) -> dict:
    """assemble() as pipeline.run calls it, fed the saved steps instead of the model's answers."""
    db = baseline()
    s1, s2 = saved["requests"], saved["operation"]
    reqs = [r | {k: v for k, v in s2["requests"][r["id"]].items() if k in ("statement", "input")}
            for r in s1["requests"]]
    return assemble.assemble(db, store_of(db), reqs, s2["requests"], saved["sources"]["requests"],
                             saved["parts"]["requests"], saved["conditions"]["requests"], saved["grouping"]["requests"],
                             [(k, saved[k]["warnings"]) for k in STEPS], order=s1.get("order"))


def sql(assembly: dict) -> list[str]:
    return [q for item in assembly["program"] for q in item.get("sql", [])]


def test_every_case_is_here():
    assert [p.stem for p in CASES] == [f"A{i:02}" for i in range(1, 51)]


@pytest.mark.parametrize("path", CASES, ids=lambda p: p.stem)
def test_assembly_replays(path):
    saved = json.loads(path.read_text())["result"]["pipeline"]
    got = replay(saved)
    assert sql(got) == sql(saved["assembly"])
    assert json.loads(json.dumps(got, default=str)) == saved["assembly"]   # the ASTs, row counts, questions, errors too
