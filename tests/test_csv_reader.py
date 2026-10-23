import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from orbitrows.pipeline.store import run
from orbitrows.services.csv.reader import EncodingNeeded, first_rows, read_table
from orbitrows.services.storage import store as storage
from orbitrows.services.storage.export import export
from test_store_run import Unsure

FLAT = "tests/fixtures/cases/flat/orders_flat.csv"


def test_read_table_with_header():
    t = read_table(FLAT)
    assert t["name"] == "orders_flat" and t["delimiter"] == "," and t["encoding"] == "utf-8"
    assert t["has_header"] and t["header"][0] == "order_id" and t["rows"][0][0] == "101"


def test_has_header_can_be_forced(tmp_path):
    f = tmp_path / "t.csv"
    f.write_text("a,b\n1,2\n")
    t = read_table(f, has_header=False)
    assert t["header"] == ["col_1", "col_2"] and t["rows"] == [["a", "b"], ["1", "2"]]


def test_semicolon_and_windows_1252(tmp_path):
    f = tmp_path / "t.csv"
    f.write_bytes("nom;prix\nCafé;3\nThé;4\n".encode("cp1252"))
    with pytest.raises(EncodingNeeded) as needed:
        read_table(f, has_header=True)
    assert any(o["answer"] == "cp1252" and "Café" in o["preview"] for o in needed.value.options)
    t = read_table(f, has_header=True, encoding="cp1252")
    assert t["encoding"] == "cp1252" and t["delimiter"] == ";" and t["rows"][0] == ["Café", "3"]


@pytest.mark.parametrize(("encoding", "value"), [
    ("mac_roman", "Café crème"), ("cp1251", "Сахар"), ("cp1254", "Şeker"),
    ("cp1250", "Łódź"), ("latin-1", "Grüße"),
])
def test_legacy_encodings_are_never_guessed_silently(tmp_path, encoding, value):
    f = tmp_path / "t.csv"
    f.write_bytes(f"name;price\n{value};3\n".encode(encoding))
    with pytest.raises(EncodingNeeded) as needed:
        read_table(f, has_header=True)
    assert any(option["answer"] == encoding and value in option["preview"] for option in needed.value.options)
    assert read_table(f, has_header=True, encoding=encoding)["rows"][0][0] == value


TEXT = ("TEXT", {"TEXT": 0.9})


async def through_the_store(tmp_path, monkeypatch, raw, name="t.csv"):
    """The file imported (classify → build → write) and exported back: (the exported bytes, the upload's table)."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    path = tmp_path / name
    path.write_bytes(raw)
    # the model says neither column is ever empty: the blank row's empty cells decide, so the store is kept
    saved = await run.classify(Unsure({"header": 0.95, "k1_type": TEXT, "k2_type": TEXT,
                                           "k1_nullable": 0.1, "k2_nullable": 0.1}), [path])
    storage.copy_files("st_rt", [path])
    storage.write("st_rt", run.build(saved)["store"], run.rows([path], saved))
    with closing(sqlite3.connect(storage.folder("st_rt") / "session.db")) as db:
        [written] = export(db, storage.folder("st_rt"), tmp_path / "out")
    return Path(written["path"]).read_bytes()


@pytest.mark.usefixtures("no_gliner")
async def test_a_line_break_inside_a_cell_survives_the_export(tmp_path, monkeypatch):
    raw = b'id,note\r\n1,"one\r\ntwo"\r\n2,x\r\n'
    f = tmp_path / "t.csv"
    f.write_bytes(raw)
    assert read_table(f, has_header=True)["rows"] == [["1", "one\r\ntwo"], ["2", "x"]]
    assert await through_the_store(tmp_path, monkeypatch, raw) == raw


@pytest.mark.usefixtures("no_gliner")
async def test_a_blank_row_between_records_is_kept_and_exported_but_a_trailing_one_is_not(tmp_path, monkeypatch):
    out = await through_the_store(tmp_path, monkeypatch, b"id,note\n1,a\n\n2,x\n\n")
    assert out == b"id,note\n1,a\n,\n2,x\n"


@pytest.mark.usefixtures("no_gliner")
async def test_utf_16_is_read_and_written_back_and_an_ambiguous_encoding_is_asked(tmp_path, monkeypatch):
    raw = "nom,prix\nCafé,3\nThé,4\n".encode("utf-16")                 # BOM, native byte order
    assert await through_the_store(tmp_path, monkeypatch, raw) == raw
    bad = tmp_path / "bad.csv"
    bad.write_bytes(b"a,b\n\x81,2\n")                                  # 0x81 is undefined in Windows-1252
    with pytest.raises(EncodingNeeded):
        read_table(bad)


def test_first_rows():
    r = first_rows(FLAT, n=2)
    assert len(r["rows"]) == 2 and r["rows"][0][0] == "order_id" and r["sniffer_header"] in (True, False, None)
