"""units/dates with Jev's classification given (no model): a period, a length of time from the session date, an unsure
answer asked, the day/month order always the user's; and a period as a condition's bounds."""
from datetime import date

from orbitrows.pipeline.prompt.conditions import predicate
from orbitrows.units import dates


def sure(**labels):
    return {f"k|{n}": (v, {v: 1.0 if n == "order" else 0.95}) for n, v in labels.items()}


def test_dates_read_with_their_classification(monkeypatch):
    monkeypatch.setattr(dates, "TODAY", date(2026, 10, 27))
    assert dates.read("last month", sure(kind="period", anchor="relative", direction="past"), "k") == {
        "kind": "period", "start": "2026-09-01", "end": "2026-10-01"}
    v = dates.read("twenty-one days earlier", sure(kind="duration", direction="past"), "k")   # number words; the
    assert (v["quantity"], v["unit"], v["date"], v["length"]) == (21, "days", "2026-10-06", 15)   # longest start read
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
