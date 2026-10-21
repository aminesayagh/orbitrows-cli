import json
from pathlib import Path

from orbitrows.pipeline.store import run
from orbitrows.services.model.client import Model

CASES = Path(__file__).parent / "fixtures" / "cases"
SECTIONS = {"read", "schema", "profile", "context", "roles", "keys", "joins"}


class Generic(Model):
    """Answers every question the same way: a choice takes its first option (0.9), a yes/no is no (0.1)."""

    def __init__(self):
        self.calls = 0

    async def answer(self, state, questions):
        self.calls += 1
        return {k: (next(iter(q["criteria"])), {next(iter(q["criteria"])): 0.9}) if q["type"] == "choice" else 0.1
                for k, q in questions.items()}, (None, None)


async def test_classify_runs_every_step_on_every_file():
    paths = sorted((CASES / "french").glob("*.csv"))
    steps, jev = [], Generic()
    saved = await run.classify(jev, paths, step=steps.append)
    assert list(saved) == [p.name for p in paths] and len(paths) == 2
    for f, s in saved.items():
        assert set(s) == SECTIONS, f
        assert {"header", "has_header", "probability"} <= set(s["read"])
        assert all("warnings" in s[name] for name in run.STEPS)
    assert jev.calls > 0 and "keys between the files" in steps
    built = run.build(saved, run.rows(paths, saved))         # the chain goes through to the proof, which catches
    assert any("datatype mismatch" in d.get("problem", "") for d in built["diffs"])   # the stub's INTEGER for text


def fixture(case):
    saved = json.loads((CASES / case / "result.json").read_text())
    return {f: s for f, s in saved["tables"].items() if f != "incoming.csv"}


def test_build_collects_every_warning():
    saved = fixture("shop")
    saved["orders.csv"]["read"] |= {"warning": "header row uncertain: first row kept as header", "probability": 0.6}
    saved["orders.csv"]["schema"]["warnings"].append({"column": "total", "type": "REAL", "count": 1,
                                                      "rows": [{"row": 2, "value": "x", "problem": "does not fit REAL"}]})
    saved["orders.csv"]["roles"]["warnings"].append({"column": "status", "problem": "role uncertain", "answer": "none"})
    saved["customers.csv"]["joins"]["warnings"].append({"column": "city", "problem": "joined record uncertain: not split"})
    saved["products.csv"]["keys"]["warnings"].append({"column": None, "problem": "no single-column primary key"})
    rows = run.rows([CASES / "shop" / f for f in saved], saved)
    built = run.build(saved, rows)
    assert set(built) == {"store", "diffs", "warnings"}
    assert [d for d in built["diffs"] if d["kind"] == "value"] == []
    got = {(w["file"], w["step"], w["problem"]) for w in built["warnings"]}
    assert got >= {("orders.csv", "header", "header row uncertain: first row kept as header"),
                   ("orders.csv", "schema", "1 values do not fit REAL: does not fit REAL"),
                   ("orders.csv", "roles", "role uncertain"),
                   ("customers.csv", "joins", "joined record uncertain: not split"),
                   ("products.csv", "keys", "no single-column primary key")}
    assert next(w for w in built["warnings"] if w["problem"] == "role uncertain")["answer"] == "none"   # nothing dropped


def test_build_reports_the_schema_problems():
    saved = fixture("shop")
    columns = saved["orders.csv"]["schema"]["create_table"]["definition"]["columns"]
    next(c for c in columns if c["column_id"] == "c_customer_id")["sql_type"] = "TEXT"   # references an INTEGER key
    built = run.build(saved, {f: [] for f in saved})
    assert {"file": "orders.csv", "step": "schema", "table_id": "t_orders",
            "problem": "TYPE_MISMATCH c_customer_id TEXT → t_customers.c_customer_id INTEGER"} in built["warnings"]
