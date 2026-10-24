import sqlite3

import pytest

from orbitrows.pipeline.merge import convert
from orbitrows.services import questions
from orbitrows.services.storage import incoming, store
from orbitrows.sql.dml import Arithmetic, Negate


def test_parse_takes_arithmetic_on_x_only():
    assert convert.parse("x * 10.85") == Arithmetic(type="arithmetic", operator="*", left=convert.X,
                                                    right={"type": "literal", "value": 10.85})
    assert convert.parse("(x - 32) / 1.8").left.operator == "-"
    assert isinstance(convert.parse("-x + 2").left, Negate)
    for bad in ("__import__('os')", "x ** 2", "y * 2", "abs(x)"):
        with pytest.raises(ValueError, match="e.g. x"):
            convert.parse(bad)
    q = {"id": "c_1", "question": {"type": "formula"}}            # the question flow: refused with an example
    assert questions.match(q, " x / 10 ") == "x / 10"
    with pytest.raises(questions.Invalid, match="e.g. x"):
        questions.match(q, "y * 2")


def test_plan_asks_for_differing_units_and_refuses_row_varying_ones():
    db = sqlite3.connect(":memory:")
    db.executescript(store.SCHEMA)
    db.executescript(incoming.SCHEMA)
    db.executescript("""
        INSERT INTO _history VALUES (1, 'now', 'import', NULL, '[]', '[]', NULL, 1);
        INSERT INTO _store_tables VALUES (1, 'products', NULL, 'now', 1);
        INSERT INTO _column_context VALUES (1, 'MAD', 'currency', 'header'), (2, 'kg', 'mass', 'header'),
            (3, 'EUR', 'currency', 'header'), (4, 'kg', 'mass', 'header'), (5, NULL, 'currency', 'column');
        INSERT INTO _store_columns (id, table_id, sql_name, sql_type, source_header, unit_id, edited_by)
            VALUES (1, 1, 'price', 'REAL', 'Prix (DH)', 1, 1), (2, 1, 'weight', 'REAL', 'Poids (kg)', 2, 1),
                   (3, 1, 'cost', 'REAL', 'Cost', 1, 1);
        INSERT INTO _incoming_tables VALUES (1, 'in_supplier_1', 'incoming/1/supplier.csv', 'now', 1);
        INSERT INTO _incoming_columns (id, table_id, sql_name, sql_type, source_header, position, unit_id,
                                       context_column_id)
            VALUES (1, 1, 'price', 'TEXT', 'Price (€)', 0, 3, NULL), (2, 1, 'weight', 'TEXT', 'Weight kg', 1, 4, NULL),
                   (3, 1, 'currency', 'TEXT', 'Currency', 2, NULL, NULL), (4, 1, 'cost', 'TEXT', 'Cost', 3, 5, 3);
        CREATE TABLE in_supplier_1 (price TEXT, weight TEXT, currency TEXT, cost TEXT);
        INSERT INTO in_supplier_1 VALUES ('', '1', 'USD', '3'), ('12.5', '2', 'EUR', '4'), ('7', '3', 'USD', '5');
    """)
    plans = [convert.plan(db, 1, pair) for pair in ((1, 1), (2, 2), (4, 3))]   # the pairs a message resolved
    found = {k: [x for p in plans for x in p[k]] for k in plans[0]}
    assert found["needs"] == [{"pair": (1, 1), "from": "EUR", "to": "MAD", "kind": "currency",
                               "samples": ["12.5", "7"]}]
    assert found["same"] == [(2, 2)]
    assert [r["pair"] for r in found["refused"]] == [(4, 3)]
    asked = convert.question(db, found["needs"][0])
    assert asked["question"] == {"type": "formula"} and "Price (€) (supplier.csv) is in EUR" in asked["problem"]
    convert.save(db, 1, "EUR", "MAD", "x * 10.85")             # answered once: not asked again
    assert convert.plan(db, 1, (1, 1))["known"] == [{"pair": (1, 1), "formula": "x * 10.85"}]
