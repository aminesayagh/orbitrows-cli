from textual_autocomplete import TargetState

from orbitrows.services.cli.app import OrbitRowsApp
from orbitrows.services.cli.screens import CsvPathAutoComplete, SourceScreen


async def test_intro_then_source(tmp_path):
    (tmp_path / "data").mkdir()
    source = tmp_path / "data" / "source.csv"
    source.write_text("sku;name;price\n001;Tee;9,90\n")
    (tmp_path / "notes.txt").write_text("x")
    (tmp_path / "empty.csv").write_text("")

    app = OrbitRowsApp(tmp_path)
    async with app.run_test() as pilot:
        await pilot.press("enter")  # leave the intro
        screen = app.screen
        assert isinstance(screen, SourceScreen)

        complete = screen.query_one(CsvPathAutoComplete)
        names = [c.value for c in complete.get_candidates(TargetState("", 0))]
        assert sorted(names) == ["data/", "empty.csv"]

        screen.submit("notes.txt")
        screen.submit("empty.csv")
        assert app.source is None
        screen.submit("data/source.csv")
        assert app.source == source
        assert app.rows == [{"sku": "001", "name": "Tee", "price": "9,90"}]


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
