"""units/dates with Jev's classification given (no model): a period, a length of time from the session date, an unsure
answer asked, the day/month order always the user's; and a period as a condition's bounds."""
from datetime import date

import pytest
from datephrase import EngineUnavailable, read_temporal

from orbitrows.pipeline.prompt.conditions import predicate
from orbitrows.units import dates


def duckling() -> bool:
    try:
        read_temporal("today")
        return True
    except EngineUnavailable:
        return False


NEEDS_DUCKLING = pytest.mark.skipif(not duckling(), reason="datephrase reads with the Duckling service: not running")


def sure(**labels):
    return {f"k|{n}": (v, {v: 1.0 if n == "order" else 0.95}) for n, v in labels.items()}


@NEEDS_DUCKLING
def test_dates_read_with_their_classification(monkeypatch):
    monkeypatch.setattr(dates, "TODAY", date(2026, 10, 27))
    assert dates.read("last month", sure(kind="period", anchor="relative", direction="past"), "k") == {
        "kind": "period", "start": "2026-09-01", "end": "2026-10-01"}
    v = dates.read("twenty-one days earlier", sure(kind="duration", direction="past"), "k")   # number words; the
    assert (v["quantity"], v["unit"], v["date"], v["length"]) == (21, "days", "2026-10-06", 15)   # longest start read
    for compound in ("1.5 months", "two months plus a week"):   # refused by the reader: never read in part
        assert dates.read(compound, sure(kind="duration", direction="future"), "k") is None
    v = dates.read("two months and five days", sure(kind="duration", direction="future"), "k")   # Duckling: 65 days
    assert (v["quantity"], v["unit"]) == (65, "days")
    weak = sure(kind="period", anchor="relative") | {"k|direction": ("past", {"past": 0.55, "future": 0.45})}
    assert dates.read("August", weak, "k")["ask"]["key"] == "k|direction"
    order = sure(kind="date", anchor="absolute") | {"k|order": ("DMY", {"DMY": 0.9, "MDY": 0.1})}
    assert dates.read("01/02/2026", order, "k")["ask"]["key"] == "k|order"                 # Jev's guess: asked
    assert dates.read("01/02/2026", sure(kind="date", anchor="absolute", order="DMY"), "k") == {
        "kind": "date", "date": "2026-02-01"}                                                # the user's answer


def test_a_period_is_a_condition_s_bounds():
    col, aug = {"type": "column", "source": "t", "reference": "c"}, {"type": "period", "start": "2026-08-01",
                                                                     "end": "2026-09-01"}
    bounds = lambda n: [(c["operator"], c["right"]["value"]) for c in (n["children"] if n["type"] == "all" else [n])]
    assert bounds(predicate("=", col, aug, None, None, True)) == [(">=", "2026-08-01"), ("<", "2026-09-01")]
    assert bounds(predicate("<", col, aug, None, None, True)) == [("<", "2026-08-01")]
    assert bounds(predicate(">", col, aug, None, None, True)) == [(">=", "2026-09-01")]
    assert bounds(predicate("between", col, aug, "2026-10-03", None, True)) == [(">=", "2026-08-01"),
                                                                                (("<=", "2026-10-03"))]


def test_a_request_whose_dates_can_t_be_read_is_refused_when_the_service_is_down(monkeypatch):
    from orbitrows.pipeline.prompt.conditions import read_dates

    def down(*a, **k):
        raise EngineUnavailable("no Duckling")
    monkeypatch.setattr(dates, "read_temporal", down)
    requests = [{"id": "r1", "text": "the sales of last month"}, {"id": "r2", "text": "the black variants"}]
    section = {"requests": {"r1": {}, "r2": {}}, "warnings": []}
    found = {"r1": [{"text": "last month", "start": 13, "end": 23}], "r2": []}
    out = read_dates(requests, found, sure(kind="period", anchor="relative", direction="past") | {
        "r1|t0|kind": ("period", {"period": 0.95})}, section)
    assert [r["dates"] for r in out] == [[], []]
    assert "Duckling" in section["requests"]["r1"]["refused"] and "refused" not in section["requests"]["r2"]


def test_a_date_the_reader_refuses_refuses_the_request(monkeypatch):
    """Never the request without its date: a phrase surely a date that can't be read is no condition dropped."""
    from orbitrows.pipeline.prompt.conditions import dated
    monkeypatch.setattr(dates, "read", lambda phrase, answers, key: None)
    section = {"requests": {"r1": {}}, "warnings": []}
    found = [{"text": "August", "start": 21, "end": 27}]
    assert dated({"id": "r1"}, found, {"r1|t0|kind": ("period", {"period": 0.95})}, section) == []
    assert "'August' can't be read" in section["requests"]["r1"]["refused"]
    section = {"requests": {"r1": {}}, "warnings": []}                   # Jev: not a date ("May" a name): nothing
    dated({"id": "r1"}, found, {"r1|t0|kind": ("none", {"none": 0.95})}, section)
    assert "refused" not in section["requests"]["r1"]
