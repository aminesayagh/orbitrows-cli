import pytest

from orbitrows.sql.ddl import ForeignKey, NewColumn, NewTable, TableColumns, check_references, create_table


def test_create_table_names_columns():
    node = create_table("incoming", [{"header": "Prix TTC", "sql_type": "TEXT", "nullable": False},
                                     {"header": "prix ttc", "sql_type": "REAL", "nullable": True},
                                     {"header": "", "sql_type": "INTEGER", "nullable": True}])
    assert [c.sql_name for c in node.definition.columns] == ["prix_ttc", "prix_ttc_2", "col_3"]


def test_sqlite_rejects_an_unknown_type():
    with pytest.raises(ValueError):
        NewTable(table_id="t_x", sql_name="x", columns=[NewColumn(column_id="c_a", sql_name="a", header="a",
                                                                   sql_type="DATE", nullable=True)])


def _id_table():
    return NewTable(table_id="t_a", sql_name="a", primary_key=["c_id"], columns=[
        NewColumn(column_id="c_id", sql_name="id", header="id", sql_type="INTEGER", nullable=False)])


def test_check_references_finds_a_type_mismatch():
    b = NewTable(table_id="t_b", sql_name="b", columns=[
        NewColumn(column_id="c_a_id", sql_name="a_id", header="a_id", sql_type="TEXT", nullable=True)],
        foreign_keys=[ForeignKey(columns=["c_a_id"], references=TableColumns(table="t_a", columns=["c_id"]))])
    assert check_references([_id_table(), b]) == [("t_b", "TYPE_MISMATCH c_a_id TEXT → t_a.c_id INTEGER")]


def test_unknown_key_column_is_rejected():
    with pytest.raises(ValueError):
        NewTable(table_id="t_c", sql_name="c", primary_key=["c_x"], columns=_id_table().columns)
