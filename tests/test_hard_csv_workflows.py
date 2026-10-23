"""Real CSV bytes through the CLI, every store-import phase, SQLite, and export.

Only the external judgment services are replaced: the model returns fixed, certain answers and GLiNER2 finds no
spans. This suite checks the workflow and data preservation, not the accuracy of live model judgments.
"""
import json
import os
import shutil
import signal
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import pytest

from orbitrows.services.cli import main as cli
from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import Model
from orbitrows.services.storage import registry
from orbitrows.services.storage import store as storage
from orbitrows.services.session import client as session


@dataclass(frozen=True)
class Case:
    name: str
    text: str
    encoding: str = "utf-8"
    header: bool = True
    key: str | None = None
    needle: str | None = None
    rows: int = 2


CASES = [
    Case("french_utf8", "sku,name,price\nA-1,Crème brûlée,12.50\nA-2,Café moulu,8.20\n", needle="Crème brûlée"),
    Case("arabic_utf8", "sku,name,notes\nA-1,قهوة عربية,طازج ☕\nA-2,شاي نعناع,ممتاز\n", needle="قهوة عربية"),
    Case("utf8_bom", "sku,name\nA-1,Тёплый чай\nA-2,Кофе\n", "utf-8-sig", needle="Тёплый чай"),
    Case("utf16_bom", "sku,name\nA-1,Thé vert\nA-2,Café\n", "utf-16", needle="Thé vert"),
    Case("utf32_bom", "sku,name\nA-1,Καφές\nA-2,Τσάι\n", "utf-32", needle="Καφές"),
    Case("french_windows", "sku;name;price\nA-1;Café crème;3,20\nA-2;Thé noir;4,10\n", "cp1252", needle="Café crème"),
    Case("polish_windows", "sku;name\nP-1;Łódź herbata\nP-2;Żółty kubek\n", "cp1250", needle="Łódź herbata"),
    Case("russian_windows", "sku;name\nR-1;Сахар\nR-2;Чёрный чай\n", "cp1251", needle="Чёрный чай"),
    Case("turkish_windows", "sku;name\nT-1;Şeker\nT-2;Çay\n", "cp1254", needle="Şeker"),
    Case("french_mac", "sku;name\nM-1;Café crème\nM-2;Éclair\n", "mac_roman", needle="Café crème"),
    Case("german_latin1", "sku;name\nG-1;Grüße\nG-2;Maßband\n", "latin-1", needle="Maßband"),
    Case("decimal_comma", "sku;price_eur;stock\nA-1;12,50;007\nA-2;0,99;0\n", needle="12,50"),
    Case("tab_quotes", 'sku\tname\tnote\nA-1\t"Black\ttea"\t"Box of 20"\nA-2\tGreen\tLoose\n', needle="Black\ttea"),
    Case("pipe_quotes", 'sku|name|note\nA-1|"Cup | saucer"|Set\nA-2|Mug|Single\n', needle="Cup | saucer"),
    Case("crlf_multiline", 'sku,description\r\nA-1,"First line\r\nSecond line"\r\nA-2,Plain\r\n',
         needle="First line\r\nSecond line"),
    Case("commas_and_quotes", 'sku,name,note\nA-1,"Tea, mint","Says ""fresh"""\nA-2,Coffee,Ground\n',
         needle='Says "fresh"'),
    Case("no_header", "A-1;Red scarf;19\nA-2;Blue scarf;21\n", header=False, needle="Red scarf"),
    Case("duplicate_headers", "sku,label,label\nA-1,English,French\nA-2,Small,Petit\n", needle="French"),
    Case("blank_middle_row", "sku,name\nA-1,Tea\n\nA-2,Coffee\n", needle="Coffee", rows=3),
    Case("integer_key_out_of_order", "id,name\n35,Desk\n3,Chair\n12,Lamp\n", key="id", needle="Desk", rows=3),
]


class CertainModel(Model):
    """A fixed oracle for the judgment boundary; all import stages still run their normal code."""

    def __init__(self, case: Case):
        self.case = case
        self.calls = 0

    async def answer(self, state, questions):
        self.calls += 1
        out = {}
        for key, question in questions.items():
            if question["type"] == "noul":
                out[key] = (0.05 if not self.case.header else 0.95) if key == "header" else (
                    0.95 if key.endswith("_nullable") else 0.05)
                continue
            criteria = question["criteria"]
            if "TEXT" in criteria and "INTEGER" in criteria:
                column = state["columns"][key.removesuffix("_type")]
                chosen = "INTEGER" if column == self.case.key == "id" else "TEXT"
            elif "dedicated_context" in criteria:
                chosen = "none"
            elif "record_reference" in criteria:
                column = state["columns"][key]
                chosen = "record_reference" if column == self.case.key else (
                    "name" if column.lower() in ("name", "title", "label") else "value_or_text")
            elif "individual_record" in criteria:
                chosen = "individual_record"
            elif "none" in criteria and key in state.get("tables", {}):
                columns = state["tables"][key]["columns"]
                chosen = next((k for k in criteria if columns.get(k) == self.case.key), "none")
            else:
                chosen = "none" if "none" in criteria else next(iter(criteria))
            out[key] = (chosen, {chosen: 0.99})
        return out, (None, None)


@pytest.mark.usefixtures("no_gliner")
@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_csv_import_question_database_export(case, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MODEL", "fixed-test-model")
    model = CertainModel(case)
    monkeypatch.setattr(cli, "connect", lambda: model)
    source = tmp_path / "catalog.csv"
    raw = case.text.encode(case.encoding)
    source.write_bytes(raw)
    handler = signal.getsignal(signal.SIGTERM)
    try:
        code = cli.main(["store", str(source), "-n", case.name])
        reply = capsys.readouterr()
        if case.encoding in {"cp1252", "cp1250", "cp1251", "cp1254", "mac_roman", "latin-1"}:
            assert code == 3, (case.name, reply.err)
            question = json.loads(reply.out)["questions"][0]
            assert json.loads(reply.out)["step"] == "encoding" and model.calls == 0
            assert any(o["answer"] == case.encoding and any(ord(c) > 127 for c in o["preview"])
                       for o in question["options"]), question
            code = cli.main(["answer", question["id"], case.encoding])
            reply = capsys.readouterr()
        assert code == 0, (case.name, reply.out, reply.err)
        with closing(registry.connect()) as db:
            [(store_id,)] = db.execute("SELECT id FROM store").fetchall()
        assert model.calls > 0
        folder = storage.folder(store_id)
        assert (folder / "files" / "catalog.csv").read_bytes() == raw
        with sqlite3.connect(folder / "session.db") as db:
            assert db.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("SELECT COUNT(*) FROM _history").fetchone() == (1,)
            assert db.execute("SELECT has_header FROM _export").fetchone() == (int(case.header),)
            assert db.execute("SELECT COUNT(*) FROM catalog").fetchone() == (case.rows,)
        out_dir = tmp_path / "out"
        code = cli.main(["export", "-s", store_id, "-o", str(out_dir)])
        reply = capsys.readouterr()
        assert code == 0, (case.name, reply.out, reply.err)
        result = out_dir / "catalog.csv"
        original = read_table(source, has_header=case.header, encoding=case.encoding)
        exported = read_table(result, has_header=case.header, encoding=case.encoding)
        assert exported["header"] == original["header"] and exported["rows"] == original["rows"]
        assert any(case.needle in cell for row in exported["rows"] for cell in row)
        if case.name not in ("blank_middle_row", "tab_quotes"):  # csv.writer normalizes blank rows and optional quotes
            assert result.read_bytes() == raw
    finally:
        signal.signal(signal.SIGTERM, handler)


class RelatedModel(CertainModel):
    """Certain answers for a customer/order pair, including the actual cross-file reference."""

    def __init__(self):
        super().__init__(Case("related_files", ""))

    async def answer(self, state, questions):
        out, usage = await super().answer(state, questions)
        if "tables" in state:
            for key, question in questions.items():
                criteria = question["criteria"]
                if key in state["tables"]:                    # a table's primary key
                    file = state["tables"][key]["file"]
                    wanted = "customer_id" if file == "customers" else "order_id"
                    chosen = next(k for k in criteria if state["tables"][key]["columns"].get(k) == wanted)
                elif "none" in criteria:                      # the order's customer_id references customers
                    chosen = "none"
                    source = next((t for t, s in state["tables"].items() if key in s["columns"]), None)
                    if source and state["tables"][source]["file"] == "orders" and state["tables"][source]["columns"][key] == "customer_id":
                        chosen = next(k for k in criteria if k != "none" and any(
                            s["file"] == "customers" and s["columns"].get(k) == "customer_id"
                            for s in state["tables"].values()))
                else:
                    continue
                out[key] = (chosen, {chosen: 0.99})
        else:
            for key, question in questions.items():
                if "record_reference" in question.get("criteria", {}):
                    column = state["columns"][key]
                    if column in ("customer_id", "order_id"):
                        out[key] = ("record_reference", {"record_reference": 0.99})
        return out, usage


@pytest.mark.usefixtures("no_gliner")
def test_two_related_csv_files_import_and_export_with_a_real_foreign_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("MODEL", "fixed-test-model")
    monkeypatch.setattr(cli, "connect", RelatedModel)
    files = {
        "customers.csv": "customer_id,name\nC-10,Café Atlas\nC-02,Maison Bleue\n",
        "orders.csv": "order_id,customer_id,total\nO-20,C-10,19.50\nO-03,C-02,8.00\nO-21,C-10,11.00\n",
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    handler = signal.getsignal(signal.SIGTERM)
    try:
        code = cli.main(["store", *(str(tmp_path / name) for name in files)])
        reply = capsys.readouterr()
        assert code == 0, (reply.out, reply.err)
        with closing(registry.connect()) as db:
            [(store_id,)] = db.execute("SELECT id FROM store").fetchall()
        with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
            assert {row[2] for row in db.execute("PRAGMA foreign_key_list(orders)")} == {"customers"}
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("SELECT COUNT(*) FROM orders").fetchone() == (3,)
        code = cli.main(["export", "-s", store_id, "-o", str(tmp_path / "out")])
        reply = capsys.readouterr()
        assert code == 0, (reply.out, reply.err)
        for name, content in files.items():
            assert (tmp_path / "out" / name).read_text() == content
    finally:
        signal.signal(signal.SIGTERM, handler)


@pytest.mark.usefixtures("no_gliner")
def test_import_message_undo_commit_and_export_are_one_workflow(monkeypatch, capsys):
    """Compose the importer and a real session process over the same store, with only language judgment stubbed."""
    home = Path(tempfile.mkdtemp(prefix="or"))               # Unix socket paths have a short maximum length
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "data"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / "state"))
    monkeypatch.setenv("MODEL", "fixed-test-model")
    monkeypatch.setenv("ORBITROWS_HANDLER", "session_stub:handle")
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [str(Path(__file__).parent),
                                                               os.environ.get("PYTHONPATH")])))
    monkeypatch.setattr(cli.coref, "missing", lambda: None)
    monkeypatch.setattr(cli, "connect", lambda: CertainModel(CASES[0]))
    source = home / "catalog.csv"
    source.write_text("sku,name\nA-1,Café\nA-2,Tea\n")
    handler = signal.getsignal(signal.SIGTERM)
    store_id = None
    try:
        code = cli.main(["store", str(source)])
        reply = capsys.readouterr()
        assert code == 0, reply.err
        with closing(registry.connect()) as db:
            [(store_id,)] = db.execute("SELECT id FROM store").fetchall()
        sql = "UPDATE catalog SET name = 'Coffee' WHERE sku = 'A-1'"
        code = cli.main(["message", sql, "-s", store_id])
        reply = capsys.readouterr()
        assert code == 0, reply.err
        assert json.loads(reply.out)["applied"][0]["changed"] == 1
        with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
            assert db.execute("SELECT name FROM catalog WHERE sku = 'A-1'").fetchone() == ("Café",)
        out = home / "out"
        code = cli.main(["export", "-o", str(out), "-s", store_id])
        reply = capsys.readouterr()
        assert code == 0, reply.err
        assert "Coffee" in (out / "catalog.csv").read_text()
        assert cli.main(["undo", "-s", store_id]) == 0
        capsys.readouterr()
        assert cli.main(["export", "-o", str(out), "--force", "-s", store_id]) == 0
        capsys.readouterr()
        assert (out / "catalog.csv").read_text() == source.read_text()
        assert cli.main(["message", sql, "-s", store_id]) == 0
        capsys.readouterr()
        assert cli.main(["exit", "-s", store_id]) == 0
        capsys.readouterr()
        with closing(sqlite3.connect(storage.folder(store_id) / "session.db")) as db:
            assert db.execute("SELECT name FROM catalog WHERE sku = 'A-1'").fetchone() == ("Coffee",)
            assert db.execute("SELECT COUNT(*) FROM _history").fetchone() == (2,)
    finally:
        signal.signal(signal.SIGTERM, handler)
        if store_id:
            try:
                session.ask(store_id, {"op": "exit"}, launch=False)
            except OSError:
                pass
        shutil.rmtree(home)
