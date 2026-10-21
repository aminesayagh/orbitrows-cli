"""A store's session: real server processes, with tests/session_stub.py as their handler (no model)."""

import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import pytest

from orbitrows.services.cli import main as cli
from orbitrows.services.session import client as session
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


def change(store, sql: str = "UPDATE products SET price = 1") -> None:
    assert session.ask(store.id, {"op": "message", "text": sql}, launch=False)["n"]


def test_a_change_is_kept_until_exit_and_undo_reverts_it(served, capsys):
    code, out, err = run(capsys, "message", DOUBLE)
    assert code == 0 and out == f"2 rows changed: {DOUBLE}\n" and "Change 1 made" in err, err
    assert run(capsys, "message", SELECT)[1] == "price\n20.0\n40.0\n"   # the server sees it
    assert prices(served) == [10.0, 20.0]                     # not committed

    code, out, err = run(capsys, "undo")                      # the SELECT was no change: the UPDATE is undone
    assert code == 0 and out == f"Undone: change 1 ({DOUBLE}).\n"
    assert run(capsys, "message", SELECT)[1] == "price\n10.0\n20.0\n"
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


def test_a_statement_sqlite_refuses_keeps_nothing(served, capsys):
    code, out, err = run(capsys, "message", "UPDATE products SET price = 1; UPDATE nope SET x = 1")
    assert code == 1 and err == "SQLite refused UPDATE nope SET x = 1: no such table: nope. Nothing was changed.\n"
    assert run(capsys, "message", SELECT)[1] == "price\n10.0\n20.0\n"   # the first statement was rolled back too
    assert "Nothing to undo" in run(capsys, "undo")[2]


def test_a_crash_loses_the_session_and_the_next_one_says_what(served, capsys):
    assert run(capsys, "message", "UPDATE products SET price = 0")[0] == 0
    assert run(capsys, "message", "UPDATE products SET price = 5")[0] == 0
    assert run(capsys, "undo")[0] == 0                        # undone: not lost
    first = pid(served)
    os.kill(first, signal.SIGKILL)
    assert ended(first)
    code, out, err = run(capsys, "message", SELECT)           # a new server, on the stale socket
    assert code == 0 and out == "price\n10.0\n20.0\n"
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
