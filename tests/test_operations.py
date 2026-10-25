"""The operations module (pipeline/operations): its catalog and its selection. No model loaded."""
from orbitrows.pipeline.operations.inventory import inventory


def test_the_inventory_keeps_what_sqlite_runs_and_refuses_the_rest():
    found = {r["names"][0]: r for r in inventory()}
    runs = {n for n, r in found.items() if r["sqlite"]}
    assert {"ROUND", "COALESCE", "REPLACE", "SUBSTRING", "AVG", "LIKE", "BETWEEN"} <= runs
    assert {"RANK", "PERCENT_RANK", "LAG"} <= {n for n, r in found.items() if r["kind"] == "window"}
    assert not found["MEDIAN"]["sqlite"] and not found["STDDEV"]["sqlite"]     # not in SQLite: a composition, later
