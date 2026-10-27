"""A store's session: real server processes, with tests/session_stub.py as their handler (no model); the prompt
pipeline's handler in an in-process Session, the pipeline stubbed."""

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from orbitrows.pipeline.prompt import coref, pipeline
from orbitrows.pipeline.store.profile import refresh
from orbitrows.pipeline.store.run import Paused
from orbitrows.services.cli import main as cli
from orbitrows.services.model import client as model
from orbitrows.services.session import client as session
from orbitrows.services.session import server as sessions
from orbitrows.services.storage import registry
from orbitrows.services.storage import store as storage

DOUBLE = "UPDATE products SET price = price * 2"
SELECT = "SELECT price FROM products ORDER BY id"


@pytest.fixture
def served(tmp_path, monkeypatch):
    """A ready store "shop" with products (10, 20) and the stub handler. The registry is in a short folder: a Unix
    socket's path has 107 bytes at most. Every server the test started is killed after it."""
    home = Path(tempfile.mkdtemp(prefix="or"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("ORBITROWS_HANDLER", "session_stub:handle")
    monkeypatch.setenv("MODEL", "orbitrows.services.model.client:Model")   # no key needed: never Jev
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(Path(__file__).parent),
                                                                   os.environ.get("PYTHONPATH")])))
    monkeypatch.delenv("ORBITROWS_IDLE", raising=False)
    store_id = registry.create("shop")
    folder = storage.folder(store_id)
    folder.mkdir(parents=True)
    with closing(sqlite3.connect(folder / "session.db")) as db:
        db.executescript(storage.SCHEMA + "CREATE TABLE products (id INTEGER PRIMARY KEY, price REAL);"
                                          "INSERT INTO products VALUES (1, 10), (2, 20);")
    registry.set_status(store_id, registry.READY)
    handler = signal.getsignal(signal.SIGTERM)
    store = SimpleNamespace(id=store_id, folder=folder, pids=[])
    yield store
    signal.signal(signal.SIGTERM, handler)                    # main() makes SIGTERM a Ctrl+C
    if (folder / "session.sock").exists():
        try:
            pid(store)
        except OSError:
            pass
    for p in store.pids:
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            pass
        ended(p)
    shutil.rmtree(home)


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def pid(store) -> int:
    """The running server's pid, now one to kill after the test."""
    store.pids.append(session.send(store.folder / "session.sock", {"op": "status"})["pid"])
    return store.pids[-1]


def ended(p: int, timeout: float = 10) -> bool:
    """Whether the server p ended within timeout (reaped: the CLI's servers are this process's children)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if os.waitpid(p, os.WNOHANG)[0]:
                return True
        except ChildProcessError:                             # not a child, or reaped already
            try:
                os.kill(p, 0)
            except ProcessLookupError:
                return True
        time.sleep(0.05)
    return False


def prices(store) -> list[float]:
    """What a separate connection sees: the committed prices."""
    with closing(sqlite3.connect(store.folder / "session.db")) as db:
        return [p for (p,) in db.execute(SELECT)]


def server(store, *args: str, idle: str | None = None) -> subprocess.Popen:
    """A server started as the CLI does, without a terminal; returned once it answers."""
    env = os.environ | ({"ORBITROWS_IDLE": idle} if idle else {})
    with open(store.folder / "session.log", "a") as log:
        proc = subprocess.Popen([sys.executable, "-m", "orbitrows.services.session", store.id, *args], env=env,
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    store.pids.append(proc.pid)
    deadline = time.monotonic() + 10
    while not (store.folder / "session.sock").exists() and proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert session.ask(store.id, {"op": "status"}, launch=False)["ok"]
    return proc


def seen(out: str) -> list[float]:
    """The prices a SELECT message printed: what the session sees."""
    return [p for [p] in json.loads(out)["applied"][0]["rows"]]


def change(store, sql: str = "UPDATE products SET price = 1") -> None:
    assert session.ask(store.id, {"op": "message", "text": sql}, launch=False)["n"]


def test_a_change_is_kept_until_exit_and_undo_reverts_it(served, capsys):
    code, out, err = run(capsys, "message", DOUBLE)
    assert code == 0 and "Change 1 made" in err, err
    assert json.loads(out) == {"store": served.id, "name": "shop", "notices": [],
                               "applied": [{"request": "r1", "sql": DOUBLE, "changed": 2}]}
    assert seen(run(capsys, "message", SELECT)[1]) == [20.0, 40.0]   # the server sees it
    assert prices(served) == [10.0, 20.0]                     # not committed

    code, out, err = run(capsys, "undo")                      # the SELECT was no change: the UPDATE is undone
    assert code == 0 and out == f"Undone: change 1 ({DOUBLE}).\n"
    assert seen(run(capsys, "message", SELECT)[1]) == [10.0, 20.0]
    code, out, err = run(capsys, "undo")
    assert code == 1 and "Nothing to undo" in err

    assert run(capsys, "message", "UPDATE products SET price = 99 WHERE id = 1")[0] == 0
    first = pid(served)
    assert run(capsys, "exit")[:2] == (0, "Saved.\n")
    assert ended(first) and prices(served) == [99.0, 20.0]
    with closing(sqlite3.connect(served.folder / "session.db")) as db:
        assert db.execute("SELECT name, prompt, json_ast, sql, classification_done FROM _history").fetchall() == [
            ("UPDATE products SET price = 99 WHERE id = 1",) * 2
            + ('[{"stub": 1}]', '["UPDATE products SET price = 99 WHERE id = 1"]', 1)]
    assert not (served.folder / "journal.jsonl").exists() and not (served.folder / "session.sock").exists()
    assert run(capsys, "exit")[1] == f"Nothing to save: store {served.id} (shop) has no open session.\n"
    assert run(capsys, "undo")[0] == 1                        # no session, nothing to undo


def test_export_has_the_unsaved_changes_and_structure_changes_are_refused(served, capsys, tmp_path):
    with closing(sqlite3.connect(served.folder / "session.db")) as db, db:   # products.csv's export map
        db.execute("INSERT INTO _store_tables VALUES (1, 'products', 'products.csv', '2026-10-21T00:00:00Z', 1)")
        db.execute("INSERT INTO _store_columns (id, table_id, sql_name, sql_type, edited_by) VALUES "
                   "(1, 1, 'id', 'INTEGER', 1), (2, 1, 'price', 'REAL', 1)")
        db.execute("INSERT INTO _export VALUES ('products.csv', 1, 1, ?)", (json.dumps(
            [{"header": h, "table_id": 1, "column_id": n, "via": []} for n, h in ((1, "id"), (2, "price"))]),))
    (served.folder / "files").mkdir()
    (served.folder / "files" / "products.csv").write_text("id;price\n1;10\n2;20\n")
    assert run(capsys, "message", DOUBLE)[0] == 0
    pid(served)
    code, out, err = run(capsys, "export", "-o", str(tmp_path))
    assert code == 0, err
    assert json.loads(out)["files"] == [{"file": "products.csv", "path": str(tmp_path / "products.csv"), "rows": 2}]
    assert (tmp_path / "products.csv").read_text() == "id;price\n1;20.0\n2;40.0\n"   # unsaved, in the upload's format

    code, out, err = run(capsys, "message", "-- add one\n alter TABLE products ADD COLUMN x TEXT")
    assert code == 1 and err == "Changing the table structure isn't supported yet: nothing was changed.\n"
    code, out, err = run(capsys, "message", "SELECT * FROM products")
    assert json.loads(out)["applied"][0]["columns"] == ["id", "price"]
    assert session.ask(served.id, {"op": "status"}, launch=False)["changes"] == 1


def test_a_statement_sqlite_refuses_keeps_nothing(served, capsys):
    code, out, err = run(capsys, "message", "UPDATE products SET price = 1; UPDATE nope SET x = 1")
    assert code == 1 and err == "SQLite refused UPDATE nope SET x = 1: no such table: nope. Nothing was changed.\n"
    assert seen(run(capsys, "message", SELECT)[1]) == [10.0, 20.0]   # the first statement was rolled back too
    assert "Nothing to undo" in run(capsys, "undo")[2]


def test_a_change_failing_after_its_statements_ran_keeps_nothing(served, monkeypatch):
    monkeypatch.setenv("ORBITROWS_HANDLER", "session_stub:handle_bytes")   # its ast can't be journaled as JSON
    reply = session.ask(served.id, {"op": "message", "text": DOUBLE})
    assert not reply["ok"] and "bytes" in reply["error"]
    pid(served)
    rows = session.ask(served.id, {"op": "message", "text": SELECT}, launch=False)["ran"][0]["rows"]
    assert rows == [[10.0], [20.0]]                           # the UPDATE is not visible in the session
    assert session.ask(served.id, {"op": "status"}, launch=False)["changes"] == 0
    assert not (served.folder / "journal.jsonl").exists() or not (served.folder / "journal.jsonl").read_text()


def test_a_crash_loses_the_session_and_the_next_one_says_what(served, capsys):
    assert run(capsys, "message", "UPDATE products SET price = 0")[0] == 0
    assert run(capsys, "message", "UPDATE products SET price = 5")[0] == 0
    assert run(capsys, "undo")[0] == 0                        # undone: not lost
    first = pid(served)
    os.kill(first, signal.SIGKILL)
    assert ended(first)
    code, out, err = run(capsys, "message", SELECT)           # a new server, on the stale socket
    assert code == 0 and seen(out) == [10.0, 20.0]
    assert "these changes were lost" in err and "  UPDATE products SET price = 0\n" in err and "= 5" not in err, err
    assert pid(served) != first and prices(served) == [10.0, 20.0]
    assert len(list(served.folder.glob("journal-lost-*.jsonl"))) == 1
    assert "were lost" not in run(capsys, "message", SELECT)[2]   # told once


def test_idle_saves_and_ends(served):
    proc = server(served, idle="1")
    change(served)
    assert proc.wait(timeout=10) == 0 and prices(served) == [1.0, 1.0]


@pytest.mark.parametrize("number", [signal.SIGTERM, signal.SIGHUP])
def test_sigterm_and_sighup_save_and_end(served, number):
    proc = server(served)
    change(served)
    proc.send_signal(number)
    assert proc.wait(timeout=10) == 0 and prices(served) == [1.0, 1.0]


def test_the_owner_s_terminal_closing_saves_and_ends(served):
    owner = subprocess.Popen(["sleep", "60"], start_new_session=True)   # a terminal's session leader
    try:
        proc = server(served, "--owner", str(owner.pid))
        change(served)
        time.sleep(1.5)
        assert proc.poll() is None and prices(served) == [10.0, 20.0]   # its owner is there: the session goes on
    finally:
        owner.kill()
        owner.wait()
    assert proc.wait(timeout=10) == 0 and prices(served) == [1.0, 1.0]


def test_without_an_owner_nothing_is_saved_on_its_own(served):
    proc = server(served)                                     # what a command without a terminal starts
    change(served)
    time.sleep(2.5)
    assert proc.poll() is None and prices(served) == [10.0, 20.0]


NOTICE = {"step": "values", "request": "r1", "problem": "price read as a number"}
EVERY = {"step": "assembly", "request": "r1", "problem": "changes every record of products", "rows": 2,
         "scope": "all_rows"}


async def doubling(prompt, jev, db, step=lambda text: None):
    """pipeline.run, stubbed: "broken" fails to build; else one yes/no call, a question until answered yes, then
    DOUBLE as the program."""
    if prompt == "lower all":                                 # the rows' step unsure, so every row would change
        unsure = {"request": "r1", "problem": "some records, but no condition column found"}
        return {"conditions": {"warnings": [unsure]},
                "assembly": {"program": [{"request": "r1", "ast": {"type": "update"}, "sql": [DOUBLE], "rows": 2}],
                             "questions": [EVERY], "errors": []}}
    if prompt == "broken":
        error = {"request": "r1", "stage": "sqlite", "error": "no such column: x"}
        return {"assembly": {"program": [], "questions": [{"step": "assembly"} | error], "errors": [error]}}
    a = await model.ask(jev, {"prompt": prompt}, {"double": model.yes_no("prices", "double them?")})
    if a["double"] < model.SURE:
        raise Paused("operation", [{"step": "operation", "request": "r1", "problem": "double them?", "id": "q_1a2b3c4d",
                                    "question": {"type": "yes_no", "call": a.call, "key": "double"}}], [NOTICE])
    return {"assembly": {"program": [{"request": "r1", "ast": {"type": "update"}, "sql": [DOUBLE], "rows": 2}],
                         "questions": [NOTICE, EVERY], "errors": []}}


@pytest.fixture
def piped(served, monkeypatch):
    """The default handler (the prompt pipeline, stubbed as doubling) in an in-process Session the CLI reaches through
    a socket pair; the model a stub answering 0.5, its calls in .calls."""
    calls = []

    class Unsure(model.Model):
        async def answer(self, state, questions):
            calls.append(model.call_id(state, questions))
            return {k: 0.5 for k in questions}, (None, None)

    monkeypatch.delenv("ORBITROWS_HANDLER")
    monkeypatch.setattr(pipeline, "run", doubling)
    monkeypatch.setattr(coref, "missing", lambda: None)       # CorPipe is not set up here, and never started
    monkeypatch.setattr(model, "connect", Unsure)
    s = sessions.Session(served.id, sessions.handler())

    def ask(store_id, request, launch=True):
        a, b = socket.socketpair()
        with a, b:
            a.sendall((json.dumps(request) + "\n").encode())
            s.serve(b)
            return json.loads(a.makefile("rb").readline())

    monkeypatch.setattr(session, "ask", ask)
    yield SimpleNamespace(session=s, calls=calls)
    s.end()                                                   # its socket removed
    s.db.close()


def test_a_message_waits_for_its_answers_then_is_applied(piped, served, capsys):
    code, out, err = run(capsys, "message", "double", "the prices")
    assert code == 3, err
    assert json.loads(out) == {"store": served.id, "name": "shop", "step": "operation", "notices": [NOTICE],
                               "questions": [{"id": "m_1a2b3c4d", "request": "r1", "step": "operation",
                                              "problem": "double them?", "type": "yes_no"}]}
    code, out, err = run(capsys, "answer", "m_1a2b3c4d", "maybe")
    assert code == 1 and "Answers: yes, no" in err
    code, out, err = run(capsys, "answer", "m_1a2b3c4d", "YES")   # the store: the latest with a session
    assert code == 0, err
    assert json.loads(out) == {"store": served.id, "name": "shop", "notices": [EVERY],   # NOTICE was told already
                               "applied": [{"request": "r1", "sql": DOUBLE, "changed": 2}]}
    db = piped.session.db
    assert db.execute(SELECT).fetchall() == [(20.0,), (40.0,)] and prices(served) == [10.0, 20.0]
    assert db.execute("SELECT prompt, sql FROM _history").fetchall() == [("double the prices", json.dumps([DOUBLE]))]
    assert len(piped.calls) == 1                              # the replay asked the model nothing
    assert "No message is waiting" in run(capsys, "answer", "m_1a2b3c4d", "yes", "-s", "shop")[2]


def test_a_program_with_errors_changes_nothing(piped, capsys):
    code, out, err = run(capsys, "message", "broken")
    assert code == 1 and out == "" and "r1 (sqlite): no such column: x" in err
    assert piped.session.db.execute(SELECT).fetchall() == [(10.0,), (20.0,)] and not piped.session.changes


def test_every_row_changed_while_unsure_is_confirmed_first(piped, capsys):
    code, out, err = run(capsys, "message", "lower", "all")
    [q] = json.loads(out)["questions"]
    assert code == 3 and q["id"] == "m_all_r1" and q["type"] == "yes_no" and not piped.session.changes
    assert run(capsys, "answer", "m_all_r1", "no")[0] == 0 and not piped.session.changes
    run(capsys, "message", "lower", "all")
    assert run(capsys, "answer", "m_all_r1", "yes")[0] == 0
    assert piped.session.db.execute(SELECT).fetchall() == [(20.0,), (40.0,)]


def test_a_select_sends_back_at_most_rows_rows(piped, monkeypatch):
    monkeypatch.setattr(sessions, "ROWS", 2)
    count = "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 5) SELECT i FROM n"
    ran = piped.session.apply("count", [{"request": "r1", "sql": [count]}], [])["ran"][0]
    assert ran["rows"] == [(1,), (2,)] and ran["more"] is True




def test_a_later_request_reads_the_ids_a_change_returned_as_the_program_runs(piped):
    """"Raise the prices under 15, then show those": the update's RETURNING ids are passed to the read (assembly's
    :rows_r1); the read finds the raised record although its price no longer matches the update's condition."""
    reply = piped.session.apply("raise then show", [
        {"request": "r1", "sql": ['UPDATE "products" SET price = price + 10 WHERE price < 15 RETURNING "id"']},
        {"request": "r2", "sql": ['SELECT id, price FROM "products" WHERE id IN (SELECT value FROM json_each(:rows_r1))']}],
        [])
    assert [r.get("changed", r.get("rows")) for r in reply["ran"]] == [1, [(1, 20.0)]], reply

async def _now(value):
    return value


@pytest.fixture
def supplied(served, monkeypatch):
    """products(sku, price in MAD) and an upload supplier.csv(ref, prix in EUR) linked ref → sku (the key) and
    prix → price; the prompt pipeline in an in-process Session, every step stubbed: r1 changes products.price."""
    from orbitrows.pipeline.prompt import (conditions, formula, grouping, guard, operation, parts, request_graph,
                                           source_route, sources, values)
    from orbitrows.services.storage import incoming
    with closing(sqlite3.connect(served.folder / "session.db")) as db:
        db.executescript(incoming.SCHEMA + """
            DROP TABLE products;
            CREATE TABLE products (sku TEXT PRIMARY KEY, price REAL);
            INSERT INTO products VALUES ('A1', 10), ('B2', 20), ('C3', 30);
            INSERT INTO _history VALUES (1, 'now', 'import', NULL, '[]', '[]', NULL, 1);
            INSERT INTO _store_tables VALUES (1, 'products', NULL, 'now', 1);
            INSERT INTO _column_context VALUES (1, 'MAD', 'currency', 'header'), (2, 'EUR', 'currency', 'header');
            INSERT INTO _store_columns (id, table_id, sql_name, sql_type, source_header, unit_id, edited_by)
                VALUES (1, 1, 'sku', 'TEXT', 'SKU', NULL, 1), (2, 1, 'price', 'REAL', 'Prix (DH)', 1, 1);
            INSERT INTO _incoming_tables VALUES (1, 'in_supplier_1', 'incoming/1/supplier.csv', 'now', 1);
            INSERT INTO _incoming_columns (id, table_id, sql_name, sql_type, source_header, position, unit_id,
                                           extra_json)
                VALUES (1, 1, 'ref', 'TEXT', 'Ref', 0, NULL, '{"primary_key_candidate": true}'),
                       (2, 1, 'prix', 'TEXT', 'Prix (€)', 1, 2, '{}');
            CREATE TABLE in_supplier_1 (ref TEXT, prix TEXT);
            INSERT INTO in_supplier_1 VALUES ('A1', '1.5'), ('B2', '2'), ('Z9', '9');
            UPDATE _incoming_columns SET references_column_id = 1 WHERE id = 1;""")
        refresh(db)                                           # the profile the import and the upload count
        db.commit()
    prompt = "update the prices from supplier.csv"
    sections = {request_graph: {"requests": [{"id": "r1", "text": prompt, "start": 0, "end": len(prompt)}],
                                "references": [], "warnings": []},
                operation: {"requests": {"r1": {"statement": "update"}}, "warnings": []},
                values: {"values": [], "warnings": []},
                sources: {"requests": {"r1": {"target": {"type": "database_table", "reference": "t_products"}}},
                          "warnings": []},
                parts: {"requests": {"r1": {"assignments": [{"column": "t_products.c_price", "value": None,
                                                             "pending": "formula"}]}}, "warnings": []},
                conditions: {"requests": {}, "parameters": {}, "warnings": []},
                grouping: {"requests": {}, "warnings": []}, formula: {"requests": {}, "warnings": []}}
    for module, section in sections.items():
        monkeypatch.setattr(module, "run", lambda *a, section=section, **k: _now(json.loads(json.dumps(section))))
    monkeypatch.setattr(guard, "run", lambda model, prompt: _now(None))
    monkeypatch.setattr(source_route, "run", lambda model, prompt, db: _now(
        {"store": True, "incoming": source_route.available(db), "warnings": []}))
    monkeypatch.setattr(pipeline.extractor, "connect", lambda: "gliner2")
    monkeypatch.setattr(coref, "mentions", lambda prompts, name: [[]])
    monkeypatch.setattr(coref, "missing", lambda: None)
    monkeypatch.delenv("ORBITROWS_HANDLER")
    s = sessions.Session(served.id, sessions.handler())
    yield s
    s.end()
    s.db.close()


def test_an_update_from_an_upload_takes_its_values_through_the_key_converted(supplied):
    from orbitrows.pipeline.merge import convert
    convert.save(supplied.db, 2, "EUR", "MAD", "x * 10")
    reply = supplied.message("update the prices from supplier.csv")
    [ran] = reply["ran"]
    assert ran["sql"].startswith("UPDATE") and "in_supplier_1" in ran["sql"] and ran["changed"] == 2, reply
    assert supplied.db.execute("SELECT sku, price FROM products ORDER BY sku").fetchall() == [
        ("A1", 15.0), ("B2", 20.0), ("C3", 30.0)]                # C3 is not in the file: unchanged


def test_a_repeating_reference_beside_a_unique_key_still_updates(supplied):
    from orbitrows.pipeline.merge import convert
    convert.save(supplied.db, 2, "EUR", "MAD", "x * 10")
    supplied.db.executescript("""
        ALTER TABLE in_supplier_1 ADD COLUMN alt TEXT;
        UPDATE in_supplier_1 SET alt = 'A1';
        INSERT INTO _incoming_columns (id, table_id, sql_name, sql_type, source_header, position, references_column_id)
            VALUES (3, 1, 'alt', 'TEXT', 'Alt', 2, 1);""")         # also references a store key, repeating
    refresh(supplied.db)
    reply = supplied.message("update the prices from supplier.csv")
    assert reply["ran"][0]["changed"] == 2, reply                  # matched on ref, which is on a row each


def test_a_missing_conversion_is_asked_then_applied(supplied):
    reply = supplied.message("update the prices from supplier.csv")
    [q] = reply["questions"]
    assert q["id"].startswith("m_c_") and q["type"] == "formula" and "EUR" in q["problem"], reply
    assert supplied.db.execute("SELECT price FROM products ORDER BY sku").fetchall() == [(10.0,), (20.0,), (30.0,)]
    reply = supplied.answer(q["id"], "x * 10")
    assert reply["ran"][0]["changed"] == 2, reply
    assert supplied.db.execute("SELECT price FROM products ORDER BY sku").fetchall() == [(15.0,), (20.0,), (30.0,)]


@pytest.fixture
def adding(served, monkeypatch):
    """products(sku, name, price in MAD) with A1, B2 and an upload supplier.csv(ref, nom, prix in EUR; a row without
    a ref) linked ref → sku (the key), nom → name, prix → price, x * 10; r1 inserts into products, the pipeline
    stubbed. → make(stock): the Session, products with a NOT NULL stock column the upload has no link to."""
    from orbitrows.pipeline.merge import convert
    from orbitrows.pipeline.prompt import (conditions, formula, grouping, operation, parts, request_graph,
                                           source_route, sources, values)
    from orbitrows.services.storage import incoming
    made = []

    def make(stock=False):
        with closing(sqlite3.connect(served.folder / "session.db")) as db:
            db.executescript(incoming.SCHEMA + f"""
                DROP TABLE products;
                CREATE TABLE products (sku TEXT PRIMARY KEY, name TEXT, price REAL{", stock INTEGER NOT NULL" * stock});
                INSERT INTO products VALUES ('A1', 'Tee', 10{", 1" * stock}), ('B2', 'Cap', 20{", 1" * stock});
                INSERT INTO _history VALUES (1, 'now', 'import', NULL, '[]', '[]', NULL, 1);
                INSERT INTO _store_tables VALUES (1, 'products', NULL, 'now', 1);
                INSERT INTO _column_context VALUES (1, 'MAD', 'currency', 'header'), (2, 'EUR', 'currency', 'header');
                INSERT INTO _store_columns (id, table_id, sql_name, sql_type, source_header, unit_id, edited_by)
                    VALUES (1, 1, 'sku', 'TEXT', 'SKU', NULL, 1), (2, 1, 'name', 'TEXT', 'Name', NULL, 1),
                           (3, 1, 'price', 'REAL', 'Prix (DH)', 1, 1);
                INSERT INTO _incoming_tables VALUES (1, 'in_supplier_1', 'incoming/1/supplier.csv', 'now', 1);
                INSERT INTO _incoming_columns (id, table_id, sql_name, sql_type, source_header, position, unit_id,
                                               extra_json)
                    VALUES (1, 1, 'ref', 'TEXT', 'Ref', 0, NULL, '{{"primary_key_candidate": true}}'),
                           (2, 1, 'nom', 'TEXT', 'Nom', 1, NULL, '{{}}'), (3, 1, 'prix', 'TEXT', 'Prix (€)', 2, 2, '{{}}');
                CREATE TABLE in_supplier_1 (ref TEXT, nom TEXT, prix TEXT);
                INSERT INTO in_supplier_1 VALUES ('A1', 'Tee v2', '1.5'), ('C3', 'Mug', '2'), ('D4', 'Bag', '3'),
                                                 ('', 'Blank', '4');
                UPDATE _incoming_columns SET references_column_id = 1 WHERE id = 1;""")
            convert.save(db, 3, "EUR", "MAD", "x * 10")
            refresh(db)
            db.commit()
        prompt = "add the new products from supplier.csv"
        sections = {request_graph: {"requests": [{"id": "r1", "text": prompt, "start": 0, "end": len(prompt)}],
                                    "references": [], "warnings": []},
                    operation: {"requests": {"r1": {"statement": "insert"}}, "warnings": []},
                    values: {"values": [], "warnings": []},
                    sources: {"requests": {"r1": {"target": {"type": "database_table", "reference": "t_products"}}},
                              "warnings": []},
                    parts: {"requests": {"r1": {"insert": {"columns": [], "values": [], "gives": []}}},
                            "warnings": []},
                    conditions: {"requests": {}, "parameters": {}, "warnings": []},
                    grouping: {"requests": {}, "warnings": []}, formula: {"requests": {}, "warnings": []}}
        for module, section in sections.items():
            monkeypatch.setattr(module, "run", lambda *a, section=section, **k: _now(json.loads(json.dumps(section))))
        monkeypatch.setattr(source_route, "run", lambda model, prompt, db: _now(
            {"store": True, "incoming": source_route.available(db), "warnings": []}))
        monkeypatch.setattr(pipeline.extractor, "connect", lambda: "gliner2")
        monkeypatch.setattr(coref, "mentions", lambda prompts, name: [[]])
        monkeypatch.setattr(coref, "missing", lambda: None)
        monkeypatch.delenv("ORBITROWS_HANDLER")
        made.append(sessions.Session(served.id, sessions.handler()))
        return made[-1]
    yield make
    for s in made:
        s.end()
        s.db.close()


def test_an_insert_from_an_upload_adds_its_new_records_converted_and_undo_removes_them(adding):
    s = adding()
    reply = s.message("add the new products from supplier.csv")
    [ran] = reply["ran"]
    assert ran["sql"].startswith('INSERT INTO "products"') and 'NOT "i"."ref" IN (SELECT' in ran["sql"], reply
    assert ran["changed"] == 2, reply                          # A1 is in the store; a row without a ref is no record
    rows = "SELECT sku, name, price FROM products ORDER BY sku"
    assert s.db.execute(rows).fetchall() == [("A1", "Tee", 10.0), ("B2", "Cap", 20.0), ("C3", "Mug", 20.0),
                                             ("D4", "Bag", 30.0)]
    assert s.undo()["ok"]
    assert s.db.execute(rows).fetchall() == [("A1", "Tee", 10.0), ("B2", "Cap", 20.0)]


def test_an_insert_from_an_upload_stops_on_a_required_column_without_a_link(adding):
    s = adding(stock=True)
    reply = s.message("add the new products from supplier.csv")
    assert not reply["ok"] and "stock" in reply["error"] and "nothing is guessed" in reply["error"], reply
    assert s.db.execute("SELECT sku FROM products ORDER BY sku").fetchall() == [("A1",), ("B2",)]


def test_an_empty_cell_of_an_upload_leaves_the_store_s_value(supplied):
    from orbitrows.pipeline.merge import convert
    convert.save(supplied.db, 2, "EUR", "MAD", "x * 10")
    supplied.db.execute("UPDATE in_supplier_1 SET prix = ' ' WHERE ref = 'B2'")   # CAST('' AS REAL) would be 0
    refresh(supplied.db)
    reply = supplied.message("update the prices from supplier.csv")
    assert reply["ran"][0]["changed"] == 1, reply
    assert supplied.db.execute("SELECT sku, price FROM products ORDER BY sku").fetchall() == [
        ("A1", 15.0), ("B2", 20.0), ("C3", 30.0)]


def test_an_insert_from_an_upload_repeating_a_key_is_refused(adding):
    s = adding()
    s.db.execute("INSERT INTO in_supplier_1 VALUES ('C3', 'Mug v2', '5')")
    refresh(s.db)
    reply = s.message("add the new products from supplier.csv")
    assert not reply["ok"] and "ref of in_supplier_1 repeats 'C3'" in reply["error"], reply
    assert s.db.execute("SELECT sku FROM products ORDER BY sku").fetchall() == [("A1",), ("B2",)]


def test_an_upload_column_the_formula_step_names_sets_the_value_without_a_link(supplied, monkeypatch):
    """"set the price to the recommended price of supplier.csv": no link to products.price, but the upload's
    recommended column (TEXT in the database, its cells numbers) is a number column of the pipeline's view, and the
    formula step names it: read through the key, cast, an empty cell no write."""
    from orbitrows.pipeline.prompt import formula
    supplied.db.executescript("""
        INSERT INTO _incoming_columns (id, table_id, sql_name, sql_type, source_header, position, unit_id, extra_json)
            VALUES (3, 1, 'recommended', 'TEXT', 'Recommended (DH)', 2, 1, '{}');
        ALTER TABLE in_supplier_1 ADD COLUMN recommended TEXT;
        UPDATE in_supplier_1 SET recommended = CASE ref WHEN 'A1' THEN ' 12.5' WHEN 'B2' THEN '' ELSE '7' END;""")
    refresh(supplied.db)
    seen = []

    async def named(jev, prompt, requests, schema, sources, values, parts, *a, **k):
        seen.append(formula.table_columns(schema, "t_products"))
        [x] = parts["r1"]["assignments"]
        x.pop("pending")
        x.update({"value": {"type": "column", "source": "t_in_supplier_1", "reference": "c_recommended"},
                  "from": "formula"})
        return {"requests": {}, "warnings": []}
    monkeypatch.setattr(formula, "run", named)
    reply = supplied.message("set the price to the recommended price of supplier.csv")
    assert "t_in_supplier_1.c_recommended" in seen[0], seen
    assert reply["ran"][0]["changed"] == 1, reply
    assert supplied.db.execute("SELECT sku, price FROM products ORDER BY sku").fetchall() == [
        ("A1", 12.5), ("B2", 20.0), ("C3", 30.0)]                # B2's cell is empty; C3 is not in the file


def test_an_update_without_a_named_source_asks_the_model_once_among_the_number_columns(supplied, monkeypatch):
    """"update prices from the file": the upload's number columns are the candidates, one choice among them; the
    choice isn't kept, so the same message again resolves its own source."""
    from orbitrows.services.model import client
    supplied.db.executescript("""
        UPDATE _incoming_columns SET sql_name = 'cost', source_header = 'Cost (DH)', unit_id = 1 WHERE id = 2;
        ALTER TABLE in_supplier_1 RENAME COLUMN prix TO cost;
        INSERT INTO _incoming_columns (id, table_id, sql_name, sql_type, source_header, position, unit_id, extra_json)
            VALUES (3, 1, 'recommended', 'TEXT', 'Recommended (DH)', 2, 1, '{}');
        ALTER TABLE in_supplier_1 ADD COLUMN recommended TEXT;
        UPDATE in_supplier_1 SET recommended = CASE ref WHEN 'A1' THEN '12.5' WHEN 'B2' THEN '25' ELSE '7' END;""")
    refresh(supplied.db)
    asked = []

    class Choosing(client.Model):
        async def answer(self, state, questions):
            asked.append({k: sorted(q["criteria"]) for k, q in questions.items()})
            return {k: ("recommended", {"recommended": 0.9, "cost": 0.1}) for k in questions}, (None, None)
    monkeypatch.setattr(client, "connect", lambda name=None: Choosing())
    reply = supplied.message("update prices from the file")
    assert asked == [{"r1_s2": ["cost", "none", "recommended"]}], asked
    assert reply.get("ran") and reply["ran"][0]["changed"] == 2, reply
    assert supplied.db.execute("SELECT sku, price FROM products ORDER BY sku").fetchall() == [
        ("A1", 12.5), ("B2", 25.0), ("C3", 30.0)]
    assert supplied.message("update prices from the file")["ok"] and len(asked) == 2   # nothing kept: resolved again
