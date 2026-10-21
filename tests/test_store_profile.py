import json
from pathlib import Path

import pytest

from orbitrows.pipeline.store.profile import overlap, profile
from orbitrows.services.csv.reader import read_table

CASES = Path(__file__).parent / "fixtures" / "cases"
TABLES = [(f.parent.name, name, saved) for f in sorted(CASES.glob("*/result.json"))
          for name, saved in json.loads(f.read_text())["tables"].items()]


def test_every_fixture_table_is_covered():
    assert len(TABLES) == 31


@pytest.mark.parametrize("case, name, saved", TABLES, ids=[f"{c}/{n}" for c, n, _ in TABLES])
def test_profile_matches_the_saved_one(case, name, saved):
    table = read_table(CASES / case / name, has_header=saved["read"]["has_header"])
    assert profile(table) == saved["profile"]


def test_profile():
    t = {"header": ["id", "status", "name"], "rows": [["1", "new", "a"], ["2", " new", "b"], ["2", "", "b"]]}
    p = profile(t)["columns"]
    assert p["id"] == {"empty": 0, "distinct": 2, "max_repeat": 2, "values": ["1", "2"], "determines": ["name"]}
    assert p["status"] == {"empty": 1, "distinct": 1, "max_repeat": 2, "values": ["new"]}
    assert overlap(["1", "3", ""], ["1", "2"]) == 0.5
    big = {"header": ["x"], "rows": [[str(i)] for i in range(25_000)]}
    assert profile(big)["profiled"] == 8_334 and profile(big)["rows"] == 25_000
