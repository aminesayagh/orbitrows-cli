import copy
import json
import os
import shutil
import signal
import sqlite3
import tempfile
import threading
from contextlib import closing
from pathlib import Path

import pytest
from typesafe_sdk import TypeSafeError

import saved_cases
from orbitrows.pipeline.store.schema import _same_number
from orbitrows.pipeline.prompt import pipeline as prompt_pipeline
from orbitrows.services.cli import main as cli
from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import Model, call_id
from orbitrows.services.storage import registry
from orbitrows.services.storage import store as storage

CASES = Path(__file__).parent / "fixtures" / "cases"
SHOP = ["customers.csv", "orders.csv", "order_lines.csv", "products.csv"]
CLASSIFY = cli.run.classify                                   # the real one, before the env fixture replaces it


SAVED = saved_cases.tables("shop") | saved_cases.tables("joins_chain")   # what the model's steps decided, by file name



class IncomingModel(Model):
    """Certain answers for incoming classification; store imports use saved fixture sections."""

    async def answer(self, state, questions):
        answers = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                answers[key] = 1.0 if key == "header" else 0.0
                continue
            options = question["criteria"]
            if "dedicated_context" in options:
                choice = "none"
            elif "record_reference" in options:
                header = state["columns"][key].lower()
                choice = "record_reference" if any(word in header for word in ("ref", "sku", "id")) else "value_or_text"
            elif "individual_record" in options:
                choice = "individual_record"
            elif key == "a" and "a1" in options:
                choice = "a1"
            else:
                choice = "none" if "none" in options else next(iter(options))
            answers[key] = (choice, {choice: 1.0})
        return answers, (None, None)


class UncertainIncomingModel(IncomingModel):
    async def answer(self, state, questions):
        answers, usage = await super().answer(state, questions)
        for key, question in questions.items():
            if key == "k1" and question["type"] == "choice" and "record_reference" in question["criteria"]:
                answers[key] = ("record_reference", {"record_reference": 0.55, "name": 0.45})
        return answers, usage


class UnitIncomingModel(IncomingModel):
    async def answer(self, state, questions):
        answers, usage = await super().answer(state, questions)
        for key, question in questions.items():
            if question["type"] == "choice" and "header_context" in question["criteria"]:
                if "MAD" in state["columns"][key]:
                    answers[key] = ("header_context", {"header_context": 1.0})
        return answers, usage


class ManyMatchModel(IncomingModel):
    async def answer(self, state, questions):
        answers, usage = await super().answer(state, questions)
        for key, question in questions.items():
            if not key.startswith("i") or "store_column" not in question.get("instructions", {}):
                continue
            source = question["instructions"]["incoming_column"]["header"]
            target = question["instructions"]["store_column"]["header"]
            relation = {("sku", "sku"): "direct", ("Product Label", "name"): "partial",
                        ("Product Label", "brand"): "partial", ("Vendor", "brand"): "direct"}.get((source, target), "none")
            answers[key] = (relation, {relation: 1.0})
        return answers, usage


class UncertainMatchModel(ManyMatchModel):
    async def answer(self, state, questions):
        answers, usage = await super().answer(state, questions)
        for key, question in questions.items():
            if "store_column" in question.get("instructions", {}):
                source = question["instructions"]["incoming_column"]["header"]
                target = question["instructions"]["store_column"]["header"]
                if (source, target) == ("sku", "sku"):
                    answers[key] = ("direct", {"direct": 0.55, "none": 0.45})
        return answers, usage


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Registry, stores and crash logs in tmp_path; the model is a stub and its steps are replaced by the fixtures'
    saved sections, so no model is ever asked. Stores are in a short folder (an upload starts the store's session
    server: a Unix socket's path has 107 bytes at most); every session still open is ended after the test."""
    home = Path(tempfile.mkdtemp(prefix="or"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MODEL", "orbitrows.services.model.client:Model")
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.setattr(cli, "connect", IncomingModel)
    monkeypatch.setattr(cli.incoming_pipeline.context, "declaration", lambda header, kind=None: None)

    async def fake_classify(jev, paths, step=lambda text: None, names=None, encodings=None):
        step("classifying")
        return {p.name: copy.deepcopy(SAVED[p.name]) for p in paths}

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
    for sock in registry.home().glob("stores/*/session.sock"):
        pid = cli.session.send(sock, {"op": "status"})["pid"]
        cli.session.send(sock, {"op": "exit"})
        os.waitpid(pid, 0)                                    # the CLI's servers are this process's children
    shutil.rmtree(home)


def leave(capsys) -> None:
    """orbitrows exit: the session's uploads committed, so a separate connection sees them."""
    code, out, err = run(capsys, "exit")
    assert code == 0 and out == "Saved.\n", err


def run(capsys, *argv) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def stores() -> list[tuple]:
    with closing(registry.connect()) as db:
        return db.execute("SELECT id, name, status FROM store ORDER BY created_at, rowid").fetchall()


def test_a_file_without_data_rows_refuses_the_store(env, capsys):
    (env / "header.csv").write_text("a,b\n")
    code, out, err = run(capsys, "store", "customers.csv", "header.csv")
    assert code == 1 and "Cannot read header.csv: no data rows" in err and stores() == []


def test_legacy_encoding_waits_for_a_choice_then_exports_the_original_letters(env, capsys):
    path = env / "customers.csv"
    raw = path.read_text().encode("cp1252")
    path.write_bytes(raw)
    code, out, err = run(capsys, "store", "customers.csv")
    assert code == 3 and not err
    [(store_id, _, status)] = stores()
    assert status == registry.WAITING and not (storage.folder(store_id) / "session.db").exists()
    payload = json.loads(out)
    assert payload["step"] == "encoding" and len(payload["questions"]) == 1
    [q] = payload["questions"]
    assert q["file"] == "customers.csv" and q["type"] == "choice"
    assert any(o["answer"] == "cp1252" and "Chloé" in o["preview"] for o in q["options"])
    assert run(capsys, "answer", q["id"], "not-a-codec")[0] == 1
    assert storage.load_import(store_id)["encodings"] == {}

    code, out, err = run(capsys, "answer", q["id"], "cp1252")
    assert code == 0, err
    assert storage.load_import(store_id)["encodings"] == {"customers.csv": "cp1252"}
    assert stores()[0][2] == registry.READY
    code, out, err = run(capsys, "export", "-o", "out")
    assert code == 0, err
    assert (env / "out" / "customers.csv").read_bytes() == raw


def test_known_encoding_can_be_given_to_store(env, capsys):
    path = env / "customers.csv"
    path.write_bytes(path.read_text().encode("cp1252"))
    code, out, err = run(capsys, "store", "customers.csv", "--encoding", "cp1252")
    assert code == 0, err
    assert stores()[0][2] == registry.READY
    assert storage.load_import(stores()[0][0])["encodings"] == {"customers.csv": "cp1252"}


def test_incoming_legacy_file_shows_readings_and_accepts_encoding(env, capsys):
    assert run(capsys, "store", "customers.csv")[0] == 0
    path = env / "supplier.csv"
    path.write_bytes("ref,nom\n001,Café\n".encode("cp1252"))
    code, out, err = run(capsys, "incoming", "supplier.csv")
    assert code == 3 and not err
    question = json.loads(out)["questions"][0]
    assert question["step"] == "encoding"
    assert any(option["answer"] == "cp1252" and "Café" in option["preview"] for option in question["options"])
    code, out, err = run(capsys, "answer", question["id"], "cp1252")
    assert code == 0, err
    assert "supplier.csv: 1 rows" in out


def test_two_incoming_uploads_with_same_filename_are_independent_snapshots(env, capsys):
    assert run(capsys, "store", "customers.csv")[0] == 0
    store_id = stores()[0][0]
    path = env / "supplier.csv"
    first = path.read_bytes()
    assert run(capsys, "incoming", "supplier.csv")[0] == 0
    path.write_text("ref,prix\n001,15\n")
    assert run(capsys, "incoming", "supplier.csv")[0] == 0
    leave(capsys)

    root = storage.folder(store_id)
    with closing(sqlite3.connect(root / "session.db")) as db:
        tables = db.execute("SELECT id, sql_name, source_file FROM _incoming_tables ORDER BY id").fetchall()
        assert [t[1] for t in tables] == ["in_supplier_1", "in_supplier_2"]
        assert db.execute("SELECT ref, prix FROM in_supplier_1").fetchall() == [("001", "10")]
        assert db.execute("SELECT ref, prix FROM in_supplier_2").fetchall() == [("001", "15")]
        assert db.execute("SELECT sql_name, sql_type, source_header, position FROM _incoming_columns "
                          "WHERE table_id = 1 ORDER BY position").fetchall() == [
                              ("ref", "TEXT", "ref", 0), ("prix", "TEXT", "prix", 1)]
        assert (root / tables[0][2]).read_bytes() == first
        assert (root / tables[1][2]).read_bytes() == path.read_bytes()
        for statement in ("INSERT INTO in_supplier_1 VALUES ('002', '22')",
                          "UPDATE in_supplier_1 SET prix = '22' WHERE ref = '001'",
                          "DELETE FROM in_supplier_1 WHERE ref = '001'"):
            with pytest.raises(sqlite3.IntegrityError, match="Incoming tables are read-only"):
                db.execute(statement)
        assert db.execute("SELECT ref, prix FROM in_supplier_1").fetchall() == [("001", "10")]
    with closing(sqlite3.connect(root / "session.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM _export").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM _store_tables").fetchone()[0] == 1
        assert {n["definition"]["sql_name"] for n in prompt_pipeline.store_of(db)} == {"customers"}
    assert run(capsys, "export", "-o", "out")[0] == 0
    assert {p.name for p in (env / "out").iterdir()} == {"customers.csv"}


def test_incoming_classification_question_resumes_before_upload(env, capsys, monkeypatch):
    assert run(capsys, "store", "customers.csv")[0] == 0
    store_id = stores()[0][0]
    monkeypatch.setattr(cli, "connect", UncertainIncomingModel)
    real = cli.incoming_pipeline.classify

    async def chatty(*args):
        print("GLiNER2 config banner")                        # what loading the extractor prints
        return await real(*args)

    monkeypatch.setattr(cli.incoming_pipeline, "classify", chatty)
    code, output, error = run(capsys, "incoming", "supplier.csv")
    assert code == 3 and "GLiNER2 config banner" in error   # never on stdout: the questions JSON is all it carries
    payload = json.loads(output)
    assert payload["step"] == "roles" and payload["questions"][0]["type"] == "choice"
    with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name = '_incoming_tables'").fetchone() is None
    question = payload["questions"][0]
    code, output, error = run(capsys, "answer", question["id"], "record_reference")
    assert code == 0, error
    leave(capsys)
    with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
        role, kind, extra = db.execute("SELECT role, kind, extra_json FROM _incoming_columns "
                                       "WHERE source_header = 'ref'").fetchone()
        assert role == storage.ROLES.index("dedicated_key")
        assert kind == storage.KINDS.index("record_reference")
        assert json.loads(extra)["profile"]["distinct"] == 1 and json.loads(extra)["primary_key_candidate"]
        assert db.execute("PRAGMA table_info(in_supplier_1)").fetchall()[0][5] == 0   # flagged, never enforced


def test_repeating_incoming_key_keeps_all_rows(env, capsys):
    assert run(capsys, "store", "customers.csv")[0] == 0
    (env / "supplier.csv").write_text("ref,warehouse,qty\n001,A,4\n001,B,8\n")
    code, _, error = run(capsys, "incoming", "supplier.csv")
    assert code == 0, error
    leave(capsys)
    with closing(sqlite3.connect(storage.folder(stores()[0][0]) / "session.db")) as db:
        assert db.execute("SELECT ref, warehouse, qty FROM in_supplier_1").fetchall() == [
            ("001", "A", "4"), ("001", "B", "8")]
        assert all(row[5] == 0 for row in db.execute("PRAGMA table_info(in_supplier_1)"))


def test_incoming_key_duplicated_outside_the_profile_sample_uploads(env, capsys, monkeypatch):
    assert run(capsys, "store", "customers.csv")[0] == 0
    monkeypatch.setattr(cli.incoming_pipeline.profile, "MAX_ROWS", 2)   # profiled: rows 1 and 3, both unique
    (env / "supplier.csv").write_text("ref,prix\n001,10\n002,20\n003,30\n002,40\n")
    code, _, error = run(capsys, "incoming", "supplier.csv")
    assert code == 0, error
    leave(capsys)
    with closing(sqlite3.connect(storage.folder(stores()[0][0]) / "session.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM in_supplier_1").fetchone()[0] == 4


def test_incoming_context_unit_is_saved_with_the_profile(env, capsys, monkeypatch):
    assert run(capsys, "store", "customers.csv")[0] == 0
    # A semicolon export keeps the decimal comma inside one cell.
    (env / "supplier.csv").write_text("ref;Price (MAD)\n001;12,50\n")
    monkeypatch.setattr(cli, "connect", UnitIncomingModel)
    monkeypatch.setattr(cli.incoming_pipeline.context, "declaration",
                        lambda header, kind=None: {"kind": "currency", "unit": "MAD"} if "MAD" in header else None)
    code, _, error = run(capsys, "incoming", "supplier.csv")
    assert code == 0, error
    leave(capsys)
    with closing(sqlite3.connect(storage.folder(stores()[0][0]) / "session.db")) as db:
        unit_id, sql_type, extra = db.execute("SELECT unit_id, sql_type, extra_json FROM _incoming_columns "
                                              "WHERE source_header = 'Price (MAD)'").fetchone()
        assert sql_type == "TEXT" and json.loads(extra)["profile"]["distinct"] == 1
        assert db.execute("SELECT unit, unit_kind, unit_from FROM _column_context WHERE id = ?", (unit_id,)).fetchone() \
            == ("MAD", "currency", "header")
        assert db.execute("SELECT price_mad FROM in_supplier_1").fetchone()[0] == "12,50"


def test_an_upload_s_key_is_linked_by_its_values_and_no_other_column_is_matched(env, capsys, monkeypatch):
    """Every ref is a products.sku: the key link without a model call; cost and Vendor wait for a message."""
    assert run(capsys, "store", "products.csv")[0] == 0
    store_id = stores()[0][0]
    (env / "supplier.csv").write_text("ref,cost,Vendor\nA-1001,12.5,Logitech\nA-1002,90,Corsair\n")

    class NoMatching(IncomingModel):
        async def answer(self, state, questions):
            assert not any("store_column" in q.get("instructions", {}) for q in questions.values()), questions
            return await super().answer(state, questions)

    monkeypatch.setattr(cli, "connect", NoMatching)
    code, output, error = run(capsys, "incoming", "supplier.csv")
    assert code == 0, error
    assert "key ref → products.sku (100% of its values)" in output
    leave(capsys)
    with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
        assert db.execute("SELECT ic.source_header, sc.source_header FROM _incoming_columns ic "
                          "JOIN _store_columns sc ON sc.id = ic.references_column_id").fetchall() == [("ref", "sku")]


async def test_an_upload_gets_one_key_link_per_store_table():
    """sku and barcode both hold a store key of variants (every value found): only one is the link, sku (the upload's
    own key), so barcode stays a column a condition can compare ("barcode disagreements")."""
    from orbitrows.pipeline import incoming_match
    table = {"header": ["sku", "barcode"], "rows": [["A", "1"], ["B", "2"], ["C", "9"]]}
    saved = {"schema": {"create_table": {"definition": {"columns": [{"column_id": "c_sku"}, {"column_id": "c_barcode"}],
                                                        "primary_key": ["c_sku"]}}},
             "context": {"context_columns": {}, "source_context": {}},
             "roles": {"columns": {1: {"role": "dedicated_key"}}},
             "profile": {"columns": {0: {"max_repeat": 1}, 1: {"max_repeat": 1}}}}
    target = {"tables": [], "keys": [{"id": 1, "table": "variants", "header": "sku", "sql_type": "TEXT",
                                      "values": ["A", "B", "C"]},
                                     {"id": 2, "table": "variants", "header": "barcode", "sql_type": "TEXT",
                                      "values": ["1", "2", "9"]}]}
    found = await incoming_match.link_keys(None, table, saved, target)   # no ambiguity: no model call
    assert found["keys"] == {0: 1}


def test_two_incoming_files_can_match_one_store_table(env, capsys, monkeypatch):
    assert run(capsys, "store", "products.csv")[0] == 0
    store_id = stores()[0][0]
    monkeypatch.setattr(cli, "connect", ManyMatchModel)
    for filename in ("supplier.csv", "supplier_other.csv"):
        (env / filename).write_text("sku,Vendor\nA-1001,Logitech\n")
        assert run(capsys, "incoming", filename)[0] == 0
    leave(capsys)
    with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
        assert db.execute("SELECT COUNT(DISTINCT table_id) FROM _incoming_columns "
                          "WHERE references_column_id IS NOT NULL").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM _incoming_tables").fetchone()[0] == 2


def test_an_upload_is_a_change_of_the_open_session(env, capsys, monkeypatch):
    """With a session running, the upload is written through it: unsaved until exit, undone like a message."""
    monkeypatch.setenv("ORBITROWS_HANDLER", "session_stub:handle")   # a message is its SQL: no model
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(Path(__file__).parent),
                                                                   os.environ.get("PYTHONPATH")])))
    assert run(capsys, "store", "customers.csv")[0] == 0
    root = storage.folder(stores()[0][0])
    assert run(capsys, "message", "SELECT 1")[0] == 0        # the session runs

    def through(sql):                                         # the rows the session sees
        code, out, err = run(capsys, "message", sql)
        assert code == 0, err
        return json.loads(out)["applied"][0]["rows"]

    def outside(sql):
        with closing(sqlite3.connect(root / "session.db")) as db:
            return db.execute(sql).fetchall()

    tables = "SELECT name FROM sqlite_schema WHERE name LIKE 'in_supplier%' OR name = '_incoming_tables'"
    code, out, err = run(capsys, "incoming", "supplier.csv")
    assert code == 0 and " as in_supplier_1\n" in out, err
    assert "saved by orbitrows exit" in err
    assert through("SELECT h.name, h.prompt FROM _incoming_tables t JOIN _history h ON h.id = t.created_by") \
        == [["Upload supplier.csv", None]]
    assert through("SELECT ref, prix FROM in_supplier_1") == [["001", "10"]]
    assert outside(tables) == [] and not (root / "incoming").exists()
    assert run(capsys, "undo")[1] == "Undone: change 1 (Upload supplier.csv).\n"
    assert through(tables) == [] and through("SELECT COUNT(*) FROM _history") == [[1]]
    assert not any((root / "incoming_pending").iterdir())    # its CSV copy went with it

    assert run(capsys, "incoming", "supplier.csv")[0] == 0
    leave(capsys)
    assert outside("SELECT t.sql_name, h.name FROM _incoming_tables t JOIN _history h ON h.id = t.created_by") \
        == [("in_supplier_1", "Upload supplier.csv")]
    assert (root / "incoming" / "1" / "supplier.csv").read_bytes() == (env / "supplier.csv").read_bytes()
    assert "calls" in json.loads((root / "incoming" / "1" / "upload.json").read_text())   # its model answers kept
    assert not any((root / "incoming_pending").iterdir())


def test_uncertain_incoming_match_waits_for_user_answer(env, capsys, monkeypatch):
    assert run(capsys, "store", "products.csv")[0] == 0
    store_id = stores()[0][0]
    (env / "supplier.csv").write_text("sku,Vendor\nA-1001,Logitech\nZ-9999,Acme\n")   # half its skus: asked
    monkeypatch.setattr(cli, "connect", UncertainMatchModel)
    code, output, error = run(capsys, "incoming", "supplier.csv")
    assert code == 3 and not error
    payload = json.loads(output)
    assert payload["step"] == "matching"
    with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name = '_incoming_tables'").fetchone() is None
    question = payload["questions"][0]
    assert "sku" in question["problem"]
    code, _, error = run(capsys, "answer", question["id"], "direct")
    assert code == 0, error
    leave(capsys)
    with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
        assert db.execute("SELECT COUNT(*) FROM _incoming_columns "
                          "WHERE references_column_id IS NOT NULL").fetchone()[0] == 1   # the key only


def test_incoming_rejects_ragged_rows_without_saving_an_upload(env, capsys):
    assert run(capsys, "store", "customers.csv")[0] == 0
    (env / "supplier.csv").write_text("ref,prix\n001,10\n002,20,extra\n")
    code, _, error = run(capsys, "incoming", "supplier.csv")
    assert code == 1 and "row 3 has 3 cells; expected 2" in error
    with closing(sqlite3.connect(storage.folder(stores()[0][0]) / "session.db")) as db:
        assert db.execute("SELECT 1 FROM sqlite_schema WHERE name = '_incoming_tables'").fetchone() is None


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
    assert code == 0, err
    assert f"supplier.csv: 1 rows · 2 columns, incoming for store {second} (Orbitrow shop)" in out
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
    async def broken(jev, paths, step=lambda text: None, names=None):
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
    async def classify(jev, paths, step=lambda text: None, names=None):
        runs.append([p.parent.name for p in paths])
        if len(runs) == 1:                                    # what the model answered, as Recorded keeps it
            jev.calls |= {"c" * 16: {"k4_role": ["price", {"price": 0.5, "quantity": 0.4}]}, "h" * 16: {"header": 0.6}}
            raise cli.run.Paused("roles", [CHOICE, YES_NO], [{"file": "orders.csv", "step": "roles"} | SHOWN_NOTICE])
        return {p.name: copy.deepcopy(SAVED[p.name]) for p in paths}
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

    async def chatty(jev, paths, step=lambda text: None, names=None):
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


def test_a_table_split_out_of_a_file_is_named_or_skipped(env, capsys, monkeypatch):
    saved_steps = cli.run.classify                            # the env fixture's: the fixtures' saved sections

    async def naming(jev, paths, step=lambda text: None, names=None):   # then the real naming pause
        saved = await saved_steps(jev, paths, step)
        if questions := cli.run.unnamed(saved, names):
            raise cli.run.Paused("naming", questions, [])
        return saved
    monkeypatch.setattr(cli.run, "classify", naming)
    code, out, err = run(capsys, "store", "export.csv")
    asked = json.loads(out)
    order, customer = asked["questions"]
    assert code == cli.PAUSED and asked["step"] == "naming"
    assert (customer["type"], customer["default"], order["default"]) == ("name", "record_2", "record_1")
    code, out, err = run(capsys, "answer", customer["id"], "Record 1")
    assert code == 1 and "record_1 is another table's name" in err
    assert run(capsys, "answer", order["id"], "SKIP")[0] == cli.PAUSED
    code, out, err = run(capsys, "answer", customer["id"], "Customers")
    assert code == 0, err                                     # the replay asks no name again
    [(store_id, _, _)] = stores()
    with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
        names = {n for n, in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"export", "record_1", "customers"} <= names and "record_2" not in names
