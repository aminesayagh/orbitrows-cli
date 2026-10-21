import json
import os
import shutil
import signal
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest
from typesafe_sdk import TypeSafeError

from orbitrows.pipeline.store.schema import _same_number
from orbitrows.services.cli import main as cli
from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import Model, call_id
from orbitrows.services.storage import registry
from orbitrows.services.storage import store as storage

CASES = Path(__file__).parent / "fixtures" / "cases"
SHOP = ["customers.csv", "orders.csv", "order_lines.csv", "products.csv"]
CLASSIFY = cli.run.classify                                   # the real one, before the env fixture replaces it


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
    handler = signal.getsignal(signal.SIGTERM)
    yield tmp_path
    signal.signal(signal.SIGTERM, handler)                    # main() makes SIGTERM a Ctrl+C


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def stores() -> list[tuple]:
    with closing(registry.connect()) as db:
        return db.execute("SELECT id, name, status FROM store ORDER BY created_at, rowid").fetchall()


def test_store_incoming_message(env, capsys, monkeypatch):
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

    sent = []                                                 # the session server is tests/test_session.py's
    monkeypatch.setattr(cli.session, "ask", lambda store_id, request, launch: sent.append((store_id, request))
                        or {"ok": True, "ran": [], "notices": []})
    code, out, err = run(capsys, "message", "raise", "prices", "by", "5%", "-s", "Orbitrow shop")
    assert code == 0 and sent == [(second, {"op": "message", "text": "raise prices by 5%"})]
    assert json.loads(out) == {"store": second, "name": "Orbitrow shop", "applied": [], "notices": []}
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


@pytest.mark.parametrize("model", [""])
def test_the_key_is_needed_when_the_model_is_named_jev(env, capsys, monkeypatch, model):
    monkeypatch.setenv("MODEL", model)  # an empty MODEL means the default, Jev
    code, out, err = run(capsys, "store", "customers.csv")
    assert code == 1 and err.startswith("TYPESAFE_API_KEY is not set") and stores() == []


@pytest.mark.parametrize("step", ["build"])
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
    real = registry.set_status

    def broken(store_id, status):
        if status == registry.READY:
            raise OSError("disk full")
        real(store_id, status)

    monkeypatch.setattr(cli.registry, "set_status", broken)
    code, out, err = run(capsys, "store", "customers.csv")
    assert code == 1 and [s for _, _, s in stores()] == [registry.FAILED]


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


CHOICE = {"file": "orders.csv", "step": "roles", "column": "total", "problem": "role uncertain", "answer": "price",
          "probability": 0.5, "id": "q_choice01",
          "question": {"type": "choice", "call": "c" * 16, "key": "k4_role", "options": [
              {"answer": "price", "value": "price", "probability": 0.5},
              {"answer": "Quantity", "value": "quantity", "probability": 0.4}]}}
YES_NO = {"file": "customers.csv", "step": "header", "problem": "header row uncertain: first row kept as header",
          "probability": 0.6, "id": "q_header01", "question": {"type": "yes_no", "call": "h" * 16, "key": "header"}}
SHOWN_NOTICE = {"column": "status", "problem": "role uncertain", "probability": 0.62}   # orders.csv's roles


def pausing(runs: list):
    """A classify that pauses on CHOICE and YES_NO the first time, then returns the fixtures' sections."""
    async def classify(jev, paths, step=lambda text: None):
        runs.append([p.parent.name for p in paths])
        if len(runs) == 1:                                    # what the model answered, as Recorded keeps it
            jev.calls |= {"c" * 16: {"k4_role": ["price", {"price": 0.5, "quantity": 0.4}]}, "h" * 16: {"header": 0.6}}
            raise cli.run.Paused("roles", [CHOICE, YES_NO], [{"file": "orders.csv", "step": "roles"} | SHOWN_NOTICE])
        return {p.name: json.loads(json.dumps(SAVED[p.name])) for p in paths}
    return classify


def test_questions_pause_the_import_until_the_user_answers(env, capsys, monkeypatch):
    runs = []
    monkeypatch.setattr(cli.run, "classify", pausing(runs))
    code, out, err = run(capsys, "store", "orders.csv", "customers.csv", "-n", "shop")
    assert code == 3 and "role uncertain" not in err
    [(store_id, _, status)] = stores()
    assert status == registry.WAITING and json.loads(out) == {  # stdout is the JSON and nothing else
        "store": store_id, "name": "shop", "step": "roles",
        "questions": [{"id": "q_choice01", "file": "orders.csv", "step": "roles", "column": "total",
                       "problem": "role uncertain", "type": "choice",
                       "options": [{"answer": "price", "probability": 0.5},
                                   {"answer": "Quantity", "probability": 0.4}]},
                      {"id": "q_header01", "file": "customers.csv", "step": "header", "column": None,
                       "problem": "header row uncertain: first row kept as header", "type": "yes_no"}],
        "notices": [{"file": "orders.csv", "step": "roles", "column": "status", "problem": "role uncertain",
                     "probability": 0.62}]}
    progress = storage.load_import(store_id)
    assert progress["open"] == [CHOICE, YES_NO] and progress["files"] == ["orders.csv", "customers.csv"]
    assert runs == [["files", "files"]]                      # classify reads the store's copies
    assert "No store yet" in run(capsys, "message", "hi")[2]  # a waiting store is not ready

    code, out, err = run(capsys, "answer", "q_header01", "YES")
    assert code == 3 and json.loads(out)["questions"][0]["id"] == "q_choice01" and json.loads(out)["notices"] == []
    assert storage.load_import(store_id)["calls"]["h" * 16] == {"header": 1.0}

    SAVED["orders.csv"]["roles"]["warnings"].append(SHOWN_NOTICE)
    try:
        code, out, err = run(capsys, "answer", "q_choice01", "quantity", "-s", "shop")
    finally:
        SAVED["orders.csv"]["roles"]["warnings"].pop()
    assert code == 0, err
    assert out.splitlines()[-1] == f"Store {store_id} (shop)" and stores()[0][2] == registry.READY
    assert "role uncertain" not in out                        # shown once, with the questions
    progress = storage.load_import(store_id)                  # kept: every model answer, and the user's
    assert progress["calls"]["c" * 16]["k4_role"] == ["quantity", {"quantity": 1.0}] and progress["open"] == []
    assert [(a["id"], a["answer"]) for a in progress["answered"]] == [("q_header01", "yes"), ("q_choice01", "Quantity")]
    assert run(capsys, "answer")[2] == "No store is waiting for answers.\n"


def test_a_store_that_loses_data_fails_even_after_answers(env, capsys, monkeypatch):
    lines = (env / "export.csv").read_text().splitlines()
    lines[2] = lines[2].replace("Alice Martin", "Alice M.")
    (env / "export.csv").write_text("\n".join(lines) + "\n")
    monkeypatch.setattr(cli.run, "classify", pausing([]))
    assert run(capsys, "store", "export.csv")[0] == 3
    assert run(capsys, "answer", "q_header01", "no")[0] == 3
    code, out, err = run(capsys, "answer", "q_choice01", "price")
    assert code == 1 and "would lose data" in err and stores()[0][2] == registry.FAILED   # no answer brings it back


class Stub(Model):
    """Every answer sure (TEXT and nullable for the types, a header row), except the price column's type: INTEGER
    at 0.5, a question. Logs each call it is asked; raises TypeSafeError while `down`."""
    down = False

    def __init__(self, log):
        self.log = log

    async def answer(self, state, questions):
        if self.down:
            raise TypeSafeError("down")
        self.log.append(call_id(state, questions))
        out = {}
        for k, q in questions.items():
            if k == "k4_type":
                out[k] = ("INTEGER", {"INTEGER": 0.5, "REAL": 0.4, "TEXT": 0.1})
            elif q["type"] == "choice":
                pick = "TEXT" if k.endswith("_type") else next(iter(q["criteria"]))
                out[k] = (pick, {pick: 0.9})
            else:
                out[k] = 0.95 if k == "header" or k.endswith("_nullable") else 0.1
        return out, (None, None)


@pytest.fixture
def stub(env, monkeypatch, no_gliner):
    """The real classify, with Stub as the model: its calls in the returned log."""
    log = []
    monkeypatch.setattr(cli.run, "classify", CLASSIFY)
    monkeypatch.setattr(cli, "connect", lambda: Stub(log))
    monkeypatch.setattr(Stub, "down", False)
    shutil.copy(CASES / "store" / "store.csv", env / "store.csv")
    return log


def price_type(store_id) -> str:
    with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
        return db.execute("SELECT sql_type FROM _store_columns WHERE source_header = 'price'").fetchone()[0]


def test_an_answer_resumes_the_import_without_asking_the_model_again(stub, capsys):
    code, out, err = run(capsys, "store", "store.csv")
    assert code == 3, err
    [(store_id, _, status)] = stores()
    [q] = json.loads(out)["questions"]
    assert status == registry.WAITING and json.loads(out)["step"] == "types"
    assert (q["column"], q["type"]) == ("price", "choice")
    assert [o["answer"] for o in q["options"]] == ["INTEGER", "REAL", "TEXT"]
    asked = len(stub)

    code, out, err = run(capsys, "answer", q["id"], "REAL")
    assert code == 0, err
    assert out.splitlines()[-1] == f"Store {store_id}" and stores()[0][2] == registry.READY
    assert price_type(store_id) == "REAL"
    assert len(stub) > asked and len(set(stub)) == len(stub)  # the resume asked only calls not answered before


def test_a_wrong_id_or_answer_is_refused(stub, capsys):
    code, out, err = run(capsys, "store", "store.csv")
    [q] = json.loads(out)["questions"]
    code, out, err = run(capsys, "answer", "q_nope", "REAL")
    assert code == 1 and f"Open questions of {stores()[0][0]}: {q['id']}" in err
    code, out, err = run(capsys, "answer", q["id"], "BLOB")
    assert code == 1 and "BLOB isn't an answer" in err and "Answers: INTEGER, REAL, TEXT" in err
    code, out, err = run(capsys, "answer", q["id"])
    assert code == 1 and q["id"] in err
    assert stores()[0][2] == registry.WAITING and storage.load_import(stores()[0][0])["answered"] == []


def test_a_failed_resume_waits_and_retries(stub, capsys, monkeypatch):
    code, out, err = run(capsys, "store", "store.csv")
    [q] = json.loads(out)["questions"]
    monkeypatch.setattr(Stub, "down", True)
    code, out, err = run(capsys, "answer", q["id"], "real")
    assert code == 1 and "Jev couldn't answer" in err
    [(store_id, _, status)] = stores()
    assert status == registry.WAITING                         # the answer is kept: no need to give it again
    code, out, err = run(capsys, "answer", q["id"], "REAL")
    assert code == 1 and "has no open question" in err
    monkeypatch.setattr(Stub, "down", False)
    code, out, err = run(capsys, "answer", "-s", store_id)
    assert code == 0, err
    assert stores()[0][2] == registry.READY and price_type(store_id) == "REAL"


def test_what_the_pipeline_prints_never_reaches_stdout(env, capsys, monkeypatch):
    pause = pausing([])

    async def chatty(jev, paths, step=lambda text: None):
        print("GLiNER2 config banner")                        # what loading the extractor prints
        return await pause(jev, paths, step)

    monkeypatch.setattr(cli.run, "classify", chatty)
    code, out, err = run(capsys, "store", "orders.csv", "customers.csv")
    assert code == 3 and json.loads(out)["step"] == "roles" and "GLiNER2 config banner" in err


def test_sigterm_is_a_ctrl_c(env, capsys, monkeypatch):
    def unhandled(*args):
        raise RuntimeError("SIGTERM not handled")             # instead of killing the test run

    signal.signal(signal.SIGTERM, unhandled)
    monkeypatch.setattr(cli.run, "classify", pausing([]))
    assert run(capsys, "store", "orders.csv", "customers.csv")[0] == 3
    real = cli.run.build

    def kill(*args):
        os.kill(os.getpid(), signal.SIGTERM)
        return real(*args)

    monkeypatch.setattr(cli.run, "build", kill)
    assert run(capsys, "answer", "q_header01", "yes")[0] == 3
    code, out, err = run(capsys, "answer", "q_choice01", "price")
    assert code == 130 and "Store " not in out
    assert stores()[0][2] == registry.WAITING                 # it had paused: its answers are kept


def test_a_killed_resume_is_answered_again(env, capsys, monkeypatch):
    monkeypatch.setattr(cli.run, "classify", pausing([]))
    assert run(capsys, "store", "orders.csv", "customers.csv")[0] == 3
    [(store_id, _, _)] = stores()
    registry.set_status(store_id, registry.IMPORTING)         # what a SIGKILL during a resume leaves
    code, out, err = run(capsys, "answer", "q_header01", "yes")   # the default store too
    assert code == 3 and stores()[0][2] == registry.WAITING
    registry.set_status(store_id, registry.IMPORTING)
    code, out, err = run(capsys, "answer", "q_choice01", "price", "-s", store_id)
    assert code == 0, err
    assert stores()[0][2] == registry.READY
    registry.create()                                         # importing, nothing asked: not answer's to resume
    assert run(capsys, "answer")[2] == "No store is waiting for answers.\n"


def test_parallel_answers_wait_for_each_other(env, capsys, monkeypatch):
    monkeypatch.setattr(cli.run, "classify", pausing([]))
    assert run(capsys, "store", "orders.csv", "customers.csv")[0] == 3
    [(store_id, _, _)] = stores()
    codes = []
    answers = [threading.Thread(target=lambda a=a: codes.append(cli.answer(*a, None)))
               for a in (("q_header01", "yes"), ("q_choice01", "price"))]
    with storage.locked(store_id):                            # an answer already running on the store
        for t in answers:
            t.start()
            t.join(0.2)
            assert t.is_alive()                               # waits for it
    for t in answers:
        t.join(10)
    assert sorted(codes) == [0, 3] and stores()[0][2] == registry.READY   # one paused on the other's question
    assert sorted(a["id"] for a in storage.load_import(store_id)["answered"]) == ["q_choice01", "q_header01"]


def test_export_writes_each_file_like_its_upload(env, capsys):
    assert run(capsys, "store", *SHOP, "export.csv", "-n", "shop")[0] == 0     # export.csv: joined records
    [(store_id, _, _)] = stores()
    files = SHOP + ["export.csv"]
    code, out, err = run(capsys, "export", "-o", "out")
    assert code == 0, err
    assert json.loads(out) == {"store": store_id, "name": "shop", "files": [
        {"file": f, "path": str(env / "out" / f), "rows": len(read_table(env / f)["rows"])} for f in files]}
    for f in files:
        up, back = read_table(env / f), read_table(env / "out" / f)
        assert back["header"] == up["header"] and len(back["rows"]) == len(up["rows"])
        for u, b in zip(up["rows"], back["rows"]):            # a number as stored: 19.90 comes back 19.9
            assert all(x == y or _same_number(x, y) for x, y in zip(u, b, strict=True)), (f, u, b)
    assert (env / "out" / "customers.csv").read_bytes() == (env / "customers.csv").read_bytes()
    code, out, err = run(capsys, "export", "-o", "out")
    assert code == 1 and out == "" and "Already there" in err and "--force" in err
    assert run(capsys, "export", "-o", "out", "--force")[0] == 0
