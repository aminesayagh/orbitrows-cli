from textual_autocomplete import TargetState

from orbitrows.services.cli.app import OrbitRowsApp
from orbitrows.services.cli.screens import CsvPathAutoComplete, PromptScreen


import os

import pytest
from sqlalchemy import delete

from orbitrows.services.jev.intent import Intent


def texts(screen) -> list[str]:
    return [str(w.render()) for w in screen.query(".message")]


def test_completes_the_word_under_the_cursor(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "store.csv").write_text("a\n1\n")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "empty.csv").write_text("")
    complete = CsvPathAutoComplete("#entry", path=tmp_path)
    names = lambda text: [c.value for c in complete.get_candidates(TargetState(text, len(text)))]
    assert names("") == [] and names("fix prices ") == []  # between prompt words: no dropdown
    assert names("d") == ["data/"] and names("fix d") == ["data/"]
    assert names("fix data/") == ["store.csv"]
    assert names("fix prices") == []  # prefix only, no fuzzy match on prompt words


@pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="DATABASE_URL not set")
async def test_prompt_routing(tmp_path, monkeypatch):
    from orbitrows.services.cli import screens
    from orbitrows.services.db import connect
    from orbitrows.services.db.models import Store

    (tmp_path / "data").mkdir()
    header = "sku;name;price;c4;c5;c6;c7;c8"  # 8 columns, 25 rows: preview shows 6 and 20
    rows = ["001;Tee;9,90;;;;;", "002;Cap;;;;;;"] + [f"{n:03};Item {n};1;;;;;" for n in range(3, 26)]
    (tmp_path / "data" / "store.csv").write_text("\n".join([header, *rows]) + "\n")
    (tmp_path / "data" / "supplier.csv").write_text("ref,prix\n001,10\n")
    (tmp_path / "empty.csv").write_text("")
    answers = {"fix names": Intent(True, True, False), "hello": Intent(False, False, False),
               "update both": Intent(True, True, True),
               "go back please": Intent(False, False, False, "undo"),  # command skips the on_topic check
               "undo the store": Intent(True, True, False, "undo"),
               "show me the store": Intent(True, True, False, "preview")}
    seen = []

    async def fake_classify(client, prompt, files):
        seen.append((prompt, sorted(files)))
        return answers[prompt]

    monkeypatch.setattr(screens, "classify", fake_classify)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    app = OrbitRowsApp(tmp_path)
    try:
        async with app.run_test() as pilot:
            await pilot.press("enter")  # leave the intro
            screen = app.screen
            assert isinstance(screen, PromptScreen)

            async def send(value):
                before = len(texts(screen))
                screen.submit(value)
                await app.workers.wait_for_complete()
                await pilot.pause()
                return "\n".join(texts(screen)[before + 1 :])  # replies, without the echo

            assert "Upload a store first" in await send("fix names")  # prompt, no file, no store
            assert "Cannot read" in await send("empty.csv")
            assert "as the store" in await send("data/store.csv")  # file, no store
            assert app.files["store"]["columns"][:3] == ["sku", "name", "price"]
            assert "for the store" in await send("fix names")  # prompt on the only file
            assert "no incoming" in await send("/undo incoming")

            reply = await send("hello data/supplier.csv")  # file + prompt, with a store
            assert "as the incoming" in reply and "can't answer that" in reply
            assert "store and the incoming" in await send("update both")
            assert seen == [("fix names", ["store"]), ("hello", ["incoming", "store"]),
                            ("update both", ["incoming", "store"])]

            assert "20 of 25 rows · 6 of 8 columns" in await send("/preview")
            table = screen.query_one(screens.DataTable)
            assert table.row_count == 20 and len(table.columns) == 6
            assert table.get_row_at(1) == ["002", "Cap", "", "", "", ""]  # empty cells left out, shown blank

            assert "Undo which file?" in await send("go back please")  # both files loaded, no target
            reply = await send("undo the store")
            assert "/undo store" in reply and "Nothing to undo on the store" in reply
            assert "Latest store version" in await send("show me the store")
            assert "Nothing to undo on the store" in await send("/undo store")
            assert "0 changes removed" in await send("/reset")
            assert "isn't built yet" in await send("/merge")
            assert "Unknown command /nope" in await send("/nope")
    finally:
        if app.store_id:
            with connect().begin() as conn:
                conn.execute(delete(Store).where(Store.id == app.store_id))  # cascades to everything


def test_candidates_skip_unreadable_entries(tmp_path):
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "hidden.csv").write_text("a\n1\n")
    locked.chmod(0o444)  # listable, but entries can't be stat'ed (like /mnt/c system files)
    try:
        complete = CsvPathAutoComplete("#path", path=tmp_path)
        names = [c.value for c in complete.get_candidates(TargetState("locked/", 7))]
        assert set(names) <= {"hidden.csv"}  # no crash; type may come from dirent without stat
    finally:
        locked.chmod(0o755)


async def test_crash_shows_message_not_traceback(tmp_path, monkeypatch, capsys):
    import pytest

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    app = OrbitRowsApp(tmp_path)
    with pytest.raises(ZeroDivisionError):
        async with app.run_test() as pilot:
            app.call_later(lambda: 1 / 0)
            await pilot.pause()
    out = capsys.readouterr()
    shown = out.out + out.err
    assert "unexpected error" in shown and "Traceback" not in shown
    [log] = (tmp_path / "orbitrows").glob("crash-*.log")
    assert "ZeroDivisionError" in log.read_text() and "Traceback" in log.read_text()
