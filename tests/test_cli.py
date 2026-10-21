import json
import os
import shutil
import signal
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from typesafe_sdk import TypeSafeError

from orbitrows.services.cli import main as cli
from orbitrows.services.storage import registry
from orbitrows.services.storage import store as storage

CASES = Path(__file__).parent / "fixtures" / "cases"
SHOP = ["customers.csv", "orders.csv", "order_lines.csv", "products.csv"]


def saved(case) -> dict:
    return json.loads((CASES / case / "result.json").read_text())["tables"]


SAVED = saved("shop") | saved("joins_chain")                  # what the model's steps decided, by file name


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Registry, stores and crash logs in tmp_path; the model is a stub and its steps are replaced by the fixtures'
    saved sections, so no model is ever asked."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MODEL", "orbitrows.services.model.client:Model")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)

    async def fake_classify(jev, paths, step=lambda text: None):
        step("classifying")
        return {p.name: json.loads(json.dumps(SAVED[p.name])) for p in paths}

    monkeypatch.setattr(cli.run, "classify", fake_classify)
    for f in SHOP:
        shutil.copy(CASES / "shop" / f, tmp_path / f)
    shutil.copy(CASES / "joins_chain" / "export.csv", tmp_path / "export.csv")
    (tmp_path / "supplier.csv").write_text("ref,prix\n001,10\n")
    (tmp_path / "empty.csv").write_text("")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def stores() -> list[tuple]:
    with closing(registry.connect()) as db:
        return db.execute("SELECT id, name, status FROM store ORDER BY created_at, rowid").fetchall()


def test_store_incoming_message(env, capsys):
    code, out, err = run(capsys, "message", "hi")
    assert code == 1 and "No store yet" in err

    code, out, err = run(capsys, "store", "customers.csv", "empty.csv")
    assert code == 1 and "Cannot read empty.csv" in err and stores() == []  # every file is read before registering

    code, out, err = run(capsys, "store", *SHOP, "customers.csv")  # a repeat is read once
    assert code == 0, err
    [(first, name, status)] = stores()
    assert (name, status) == (None, 1) and first.startswith("st_")
    assert out.splitlines()[-1] == f"Store {first}"  # the last line is the store
    assert "customers: 5 rows · 4 columns, from customers.csv" in out
    assert "classifying…" in err  # progress goes to stderr

    code, out, err = run(capsys, "store", "export.csv", "-n", "Orbitrow shop")
    second = stores()[-1][0]
    assert code == 0 and out.splitlines()[-1] == f"Store {second} (Orbitrow shop)"

    code, out, err = run(capsys, "incoming", "supplier.csv")  # the latest store by default
    assert code == 0 and f"supplier.csv: 1 rows · 2 columns, incoming for store {second} (Orbitrow shop)" in out
    code, out, err = run(capsys, "incoming", "supplier.csv", "--store", first)
    assert code == 0 and f"incoming for store {first}" in out
    assert run(capsys, "incoming", "missing.csv")[2].startswith("File not found")

    code, out, err = run(capsys, "message", "raise", "prices", "by", "5%", "-s", "Orbitrow shop")
    assert code == 0 and "isn't built yet" in out and second in out
    code, out, err = run(capsys, "message", "hi", "--store", "nope")
    assert code == 1 and "No ready store named nope" in err
    assert len(stores()) == 2  # incoming and message never create a store


def test_store_is_written_to_disk(env, capsys):
    code, out, err = run(capsys, "store", *SHOP, "-n", "shop")
    assert code == 0, err
    [(store_id, name, status)] = stores()
    assert out.splitlines()[-1] == f"Store {store_id} (shop)" and status == registry.READY
    folder = storage.folder(store_id)
    assert sorted(p.name for p in (folder / "files").iterdir()) == sorted(SHOP)
    assert (folder / "original.db").is_file()
    with closing(sqlite3.connect(folder / "session.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM orders").fetchone() == (8,)
    with closing(registry.connect()) as db:
        [ast] = db.execute("SELECT schema_ast FROM store WHERE id = ?", (store_id,)).fetchone()
    tables = json.loads(ast)
    assert {t["type"] for t in tables} == {"create_table"}
    assert {t["definition"]["sql_name"] for t in tables} >= {"customers", "orders", "order_lines", "products"}
    for line in ["orders: 8 rows · 4 columns, from orders.csv", "products: 6 rows · 5 columns, from products.csv"]:
        assert line in out


def test_warnings_are_printed(env, capsys, monkeypatch):
    SAVED["orders.csv"]["roles"]["warnings"].append({"column": "status", "problem": "role uncertain", "probability": 0.62})
    try:
        code, out, err = run(capsys, "store", *SHOP)
    finally:
        SAVED["orders.csv"]["roles"]["warnings"].pop()
    assert code == 0 and "Warning: orders.csv · roles · status: role uncertain (62%)" in out.splitlines()


def test_a_store_that_loses_data_is_refused(env, capsys):
    lines = (env / "export.csv").read_text().splitlines()
    lines[2] = lines[2].replace("Alice Martin", "Alice M.")  # customer 1's name now disagrees with row 1's
    (env / "export.csv").write_text("\n".join(lines) + "\n")
    SAVED["export.csv"]["roles"]["warnings"].append({"column": "product", "problem": "role uncertain", "probability": 0.5})
    try:
        code, out, err = run(capsys, "store", "export.csv")
    finally:
        SAVED["export.csv"]["roles"]["warnings"].pop()
    assert code == 1 and "would lose data" in err
    assert "Warning: export.csv · roles · product: role uncertain (50%)" in out.splitlines()   # shown on a refusal too
    assert 'export.csv, row 2, customer_name: "Alice M." would come back as "Alice Martin"' in err
    [(store_id, _, status)] = stores()
    assert status == registry.FAILED and not (storage.folder(store_id) / "session.db").exists()


def test_files_with_the_same_name_are_refused(env, capsys):
    (env / "other").mkdir()
    shutil.copy(env / "customers.csv", env / "other" / "customers.csv")
    code, out, err = run(capsys, "store", "customers.csv", "other/customers.csv")
    assert code == 1 and "customers.csv, other/customers.csv would all be the table customers. Rename one" in err
    assert stores() == []

    shutil.copy(env / "order_lines.csv", env / "order-lines.csv")    # other names, same table
    code, out, err = run(capsys, "store", "order_lines.csv", "order-lines.csv")
    assert code == 1 and "would all be the table order_lines. Rename one" in err and stores() == []
    shutil.copy(env / "customers.csv", env / "Customers.csv")
    code, out, err = run(capsys, "store", "customers.csv", "Customers.csv")
    assert code == 1 and "all be the table customers" in err and stores() == []


def test_the_key_is_needed_for_jev(env, capsys, monkeypatch):
    monkeypatch.delenv("MODEL")
    code, out, err = run(capsys, "store", "customers.csv")
    assert code == 1 and err.startswith("TYPESAFE_API_KEY is not set") and stores() == []


@pytest.mark.parametrize("model", ["jev", ""])
def test_the_key_is_needed_when_the_model_is_named_jev(env, capsys, monkeypatch, model):
    monkeypatch.setenv("MODEL", model)  # an empty MODEL means the default, Jev
    code, out, err = run(capsys, "store", "customers.csv")
    assert code == 1 and err.startswith("TYPESAFE_API_KEY is not set") and stores() == []


@pytest.mark.parametrize("step", ["rows", "build"])
def test_ctrl_c_in_the_code_after_the_model_stops_the_import(env, capsys, monkeypatch, step):
    real = getattr(cli.run, step)

    def interrupt(*args):
        os.kill(os.getpid(), signal.SIGINT)  # a real Ctrl+C: under asyncio.run it would only cancel at the next await
        return real(*args)

    monkeypatch.setattr(cli.run, step, interrupt)
    code, out, err = run(capsys, "store", "customers.csv")
    assert code == 130 and "Store " not in out
    assert [s for _, _, s in stores()] == [registry.FAILED]


def test_a_failure_setting_the_status_marks_the_store_failed(env, capsys, monkeypatch):
    def broken(store_id, tables):
        raise OSError("disk full")

    monkeypatch.setattr(cli.registry, "ready", broken)
    code, out, err = run(capsys, "store", "customers.csv")
    assert code == 1 and [s for _, _, s in stores()] == [registry.FAILED]


def test_a_ready_store_needs_its_schema_ast(env):
    store_id = registry.create()
    with pytest.raises(sqlite3.IntegrityError):
        registry.set_status(store_id, registry.READY)


def test_same_name_needs_the_id(env, capsys):
    run(capsys, "store", "customers.csv", "-n", "shop")
    run(capsys, "store", "customers.csv", "-n", "shop")
    code, out, err = run(capsys, "message", "hi", "-s", "shop")
    assert code == 1 and "Several stores are named shop" in err


def test_store_needs_files(env, capsys):
    with pytest.raises(SystemExit) as exit:
        cli.main(["store"])
    assert exit.value.code == 2


def test_failed_import_marks_the_store_failed(env, capsys, monkeypatch):
    async def broken(jev, paths, step=lambda text: None):
        raise TypeSafeError("down")

    monkeypatch.setattr(cli.run, "classify", broken)
    code, out, err = run(capsys, "store", "customers.csv")
    assert code == 1 and "Jev couldn't answer" in err
    assert [s for _, _, s in stores()] == [-1]
    assert "No store yet" in run(capsys, "message", "hi")[2]  # a failed store is never picked


def test_crash_shows_message_not_traceback(env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "read_table", lambda path: 1 / 0)
    code, out, err = run(capsys, "store", "customers.csv")
    assert code == 1 and "unexpected error" in err and "Traceback" not in err + out
    [log] = (env / "state" / "orbitrows").glob("crash-*.log")
    assert "ZeroDivisionError" in log.read_text() and "Traceback" in log.read_text()
