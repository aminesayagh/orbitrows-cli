import saved_cases
from pathlib import Path

import pytest

from orbitrows.pipeline.store.profile import overlap, profile
from orbitrows.services.csv.reader import read_table

CASES = Path(__file__).parent / "fixtures" / "cases"
TABLES = [(f.parent.name, name, saved) for f in sorted(CASES.glob("*/result.json"))
          for name, saved in saved_cases.tables(f.parent.name).items()]
PICKED = [x for x in TABLES if x[0] in ("shop", "joins_chain")][:2]


@pytest.mark.parametrize("case, name, saved", PICKED, ids=[f"{c}/{n}" for c, n, _ in PICKED])
def test_profile_matches_the_saved_one(case, name, saved):
    table = read_table(CASES / case / name, has_header=saved["read"]["has_header"])
    got = profile(table)
    for c in got["columns"].values():
        c.pop("follows_row_order", None)                       # the PoC did not count it
    assert got == saved["profile"]


def test_profile():
    t = {"header": ["id", "status", "name"], "rows": [["1", "new", "a"], ["2", " new", "b"], ["2", "", "b"]]}
    p = profile(t)["columns"]
    assert p[0] == {"empty": 0, "distinct": 2, "max_repeat": 2, "values": ["1", "2"], "determines": [2]}   # id → name
    assert p[1] == {"empty": 1, "distinct": 1, "max_repeat": 2, "values": ["new"]}
    assert overlap(["1", "3", ""], ["1", "2"]) == 0.5
    big = {"header": ["x"], "rows": [[str(i)] for i in range(25_000)]}
    assert profile(big)["profiled"] == 8_334 and profile(big)["rows"] == 25_000


def test_follows_row_order():
    t = {"header": ["ref", "id", "stock", "code"],
         "rows": [[f"A-{1001 + i}", str(i + 1), q, f"B{i % 3}"] for i, q in enumerate("35 7 25 12 20 5 3 140 18 31".split())]}
    p = profile(t)["columns"]
    assert [p[i].get("follows_row_order", False) for i in range(4)] == [True, True, False, False]
