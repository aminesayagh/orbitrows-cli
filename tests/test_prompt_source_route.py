"""Early prompt source selection stays independent of the store SQL compiler."""

import sqlite3

import pytest

from orbitrows.pipeline.prompt import pipeline, source_route
from orbitrows.pipeline.store.run import Paused
from orbitrows.services.model.client import Model, Recorded, settle


class Sources(Model):
    def __init__(self, choices=None):
        self.choices = choices or {}

    async def answer(self, state, questions):
        out = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                out[key] = self.choices.get(key, 1.0 if key == "store" else 0.0)
            else:
                selected = self.choices.get(key, next(iter(question["criteria"])))
                out[key] = (selected, {selected: 0.6, **{k: 0.4 for k in question["criteria"] if k != selected}})
        return out, (None, None)


@pytest.fixture
def db(tmp_path):
    main = sqlite3.connect(tmp_path / "session.db")
    main.execute("CREATE TABLE products (sku TEXT PRIMARY KEY, price REAL) STRICT")
    main.execute("INSERT INTO products VALUES ('A1', 10)")
    main.commit()
    with main:
        main.execute("CREATE TABLE _incoming_tables (id INTEGER PRIMARY KEY, sql_name TEXT, source_file TEXT)")
        main.execute("CREATE TABLE _incoming_columns (table_id INTEGER, source_header TEXT, position INTEGER)")
        main.executemany("INSERT INTO _incoming_tables VALUES (?, ?, ?)", [
            (1, "in_supplier_1", "incoming/1/supplier.csv"),
            (2, "in_warehouse_2", "incoming/2/warehouse.csv")])
        main.executemany("INSERT INTO _incoming_columns VALUES (?, ?, ?)", [
            (1, "sku", 0), (1, "price", 1), (2, "sku", 0), (2, "qty", 1)])
    yield main
    main.close()


@pytest.mark.asyncio
async def test_explicit_prompt_can_select_several_incoming_sources(db):
    result = await source_route.run(Sources(), "Compare in_supplier_1 and in_warehouse_2 with our store", db)
    assert result["store"] is True
    assert [t["id"] for t in result["incoming"]] == [1, 2]


@pytest.mark.asyncio
async def test_implicit_incoming_source_can_be_selected_by_model(db):
    result = await source_route.run(Sources({"incoming_2": 1.0}), "Show counted stock beside our stock", db)
    assert [t["sql_name"] for t in result["incoming"]] == ["in_warehouse_2"]


@pytest.mark.asyncio
async def test_store_only_prompt_needs_no_incoming_upload(db):
    result = await source_route.run(Sources(), "Raise our store prices by five percent", db)
    assert result["store"] is True and result["incoming"] == [] and result["warnings"] == []


@pytest.mark.asyncio
async def test_uncertain_source_asks_and_replays_answer(db):
    inner, calls = Sources({"incoming_1": 0.6}), {}
    with pytest.raises(Paused) as paused:
        await pipeline.run("Show the supplier's prices", Recorded(inner, calls), db)
    assert paused.value.phase == "source_selection"
    [question] = paused.value.questions
    assert question["question"]["type"] == "yes_no"
    settle(calls, question["question"]["call"], question["question"]["key"], "yes")
    result = await pipeline.run("Show the supplier's prices", Recorded(inner, calls), db)
    assert [t["id"] for t in result["source_selection"]["incoming"]] == [1]
    assert result["assembly"]["program"] == []
    assert "not implemented yet" in result["assembly"]["errors"][0]["error"]


@pytest.mark.asyncio
async def test_same_filename_uploads_require_an_explicit_choice(db):
    with db:
        db.execute("INSERT INTO _incoming_tables VALUES (3, 'in_supplier_3', 'incoming/3/supplier.csv')")
    inner, calls = Sources(), {}
    with pytest.raises(Paused) as paused:
        await pipeline.run("Show supplier.csv beside our products", Recorded(inner, calls), db)
    assert paused.value.phase == "source_selection"
    [question] = paused.value.questions
    assert {o["answer"] for o in question["question"]["options"]} == {
        "in_supplier_1 (upload 1)", "in_supplier_3 (upload 3)"}
    settle(calls, question["question"]["call"], question["question"]["key"], "3")
    result = await pipeline.run("Show supplier.csv beside our products", Recorded(inner, calls), db)
    assert [t["id"] for t in result["source_selection"]["incoming"]] == [3]


@pytest.mark.asyncio
async def test_old_incoming_database_requires_reimport_before_prompt(db):
    with db:
        db.execute("DROP TABLE _incoming_columns")
        db.execute("DROP TABLE _incoming_tables")
    from pathlib import Path
    Path(db.execute("PRAGMA database_list").fetchone()[2]).with_name("incoming.db").touch()
    result = await pipeline.run("Use supplier.csv prices", Sources(), db)
    assert "need to be reimported" in result["assembly"]["errors"][0]["error"]
