"""operations/use.py: an entry filled from its own declaration and built, checked against what it must refuse (no
model loaded; its search is tests/test_catalog_search.py's, -m catalog)."""
import pytest

from fixtures.orbitrow.load import baseline
from orbitrows.pipeline.operations import use
from orbitrows.pipeline.operations.catalog import connect, get
from orbitrows.pipeline.prompt.pipeline import store_of
from orbitrows.sql.sql import program

PRICE = {"type": "column", "source": "t_variants", "reference": "c_price_mad"}


@pytest.fixture(scope="module")
def entry():
    db = connect()
    yield lambda i: get(db, i)
    db.close()


def shown(node):
    db = baseline()
    q = {"id": "q", "type": "select", "from": {"type": "database_table", "reference": "t_variants",
                                               "alias": "t_variants"},
         "columns": [{"expression": node, "alias": "v"}], "scope": {"type": "all_rows"}}
    return sorted(r[0] for r in db.execute(program([q], store_of(db))[0]))


def test_an_entry_is_filled_from_its_declaration_and_built(entry):
    inc = entry("INCREASE_PERCENT")
    assert [p["name"] for p in use.slots(inc)] == ["percent"]
    assert [p["name"] for p in use.slots(entry("FILL_EMPTY"))] == ["value"]          # its column's type: the code's
    params, problems = use.fill(inc, {"column": PRICE, "percent": 10})
    assert problems == [] and shown(use.build(inc, params)) == sorted(
        r[0] for r in baseline().execute("SELECT price_mad * 1.1 FROM variants"))
    params, _ = use.fill(entry("ADD_PREFIX"), {"column": PRICE, "text": "NEW-"})
    assert params["text"] == {"type": "literal", "value": "NEW-"}                     # a text becomes a literal


def test_what_a_declaration_refuses_is_said_never_guessed(entry):
    assert use.fill(entry("INCREASE_PERCENT"), {"column": PRICE})[1] == [
        "INCREASE_PERCENT: percent missing (The percentage the value is raised by.)"]
    assert "not one of" in use.fill(entry("SHIFT_DATE"), {"column": PRICE, "amount": 2, "unit": "weeks"})[1][0]
    assert "not a number" in use.fill(entry("MULTIPLY_BY"), {"column": PRICE, "factor": "twice"})[1][0]
    assert "not a column" in use.fill(entry("SET_FROM_COLUMN"), {"column": PRICE, "other": "cost"})[1][0]
    assert use.applies(entry("UPPER_CASE"), "text") and not use.applies(entry("UPPER_CASE"), "number")
    assert use.applies(entry("SET_VALUE"), "date") and not use.applies(entry("INCREASE_PERCENT"), "date")
