"""The SQL format's window functions (step 2.1): computed over each record's group, and a condition on one read from an
earlier select's result (SQLite refuses a window in WHERE)."""
import pytest
from pydantic import ValidationError

from fixtures.orbitrow.load import baseline
from orbitrows.pipeline.prompt.pipeline import store_of
from orbitrows.sql import dml
from orbitrows.sql.sql import STATEMENT, program

V = {"type": "database_table", "reference": "t_variants", "alias": "v"}
col = lambda c, s="v": {"type": "column", "source": s, "reference": c}
PRICE_DESC = [{"expression": col("c_price_mad"), "direction": "desc"},
              {"expression": col("c_variant_id"), "direction": "asc"}]


def test_a_window_is_computed_per_group_and_a_condition_on_it_reads_the_earlier_result():
    db = baseline()
    ranked = {"id": "r1", "type": "select", "from": V, "scope": {"type": "all_rows"}, "columns": [
        {"expression": col("c_variant_id"), "alias": "variant"},
        {"expression": {"type": "window", "function": "row_number", "partition_by": [col("c_product_id")],
                        "order_by": PRICE_DESC}, "alias": "place"},
        {"expression": {"type": "window", "function": "lag", "arguments": [col("c_price_mad")],
                        "partition_by": [col("c_product_id")], "order_by": PRICE_DESC}, "alias": "dearer"},
        {"expression": {"type": "window", "function": "min", "arguments": [col("c_price_mad")],
                        "partition_by": [col("c_product_id")], "order_by": PRICE_DESC, "whole": True},
         "alias": "cheapest"}]}
    top = {"id": "r2", "type": "select", "from": {"type": "request_result", "request_id": "r1", "alias": "r"},
           "columns": [{"expression": col("variant", "r"), "alias": "variant"}],
           "scope": {"type": "filtered", "where": {"type": "comparison", "operator": "=",
                                                   "left": col("place", "r"), "right": {"type": "literal", "value": 1}}},
           "order_by": [{"expression": col("variant", "r"), "direction": "asc"}], "limit": 3, "offset": 1}
    first, second = program([ranked, top], store_of(db))
    rows = {r[0]: r[1:] for r in db.execute(first)}
    assert rows[103] == (1, None, 120) and rows[101] == (2, 125, 120) and rows[106] == (2, 120, 80)
    assert [r[0] for r in db.execute(second)] == [104, 107, 108]   # the dearest of each product, after the first


def test_what_a_window_cannot_be_is_refused():
    window = lambda **w: STATEMENT.validate_python({"id": "q", "type": "select", "from": V, "scope": {
        "type": "all_rows"}, "columns": [{"expression": {"type": "window"} | w, "alias": "x"}]})
    with pytest.raises(ValidationError, match="needs an order"):
        window(function="rank")
    with pytest.raises(ValidationError, match="INVALID_FUNCTION_ARITY"):
        window(function="sum", order_by=PRICE_DESC)
    assert set(dml.ORDERED) <= set(dml.WINDOW)
