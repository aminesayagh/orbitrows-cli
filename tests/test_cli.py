from contextlib import closing

import pytest
from typesafe_sdk import TypeSafeError

from orbitrows.pipeline.context import ColumnProfile, Conflict
from orbitrows.services.cli import main as cli
from orbitrows.services.storage import registry


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Registry and crash logs in tmp_path, a fake key, and Jev's profiling replaced."""
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("OPENROUTER_API_KEY", "test")
    monkeypatch.setattr(cli, "connect", lambda: None)

    async def fake_columns(jev, header, rows, step=lambda text: None):
        step("Jev: classifying column context")
        context = {f"c{i}": ColumnProfile("none", 0.9) for i in range(1, len(header) + 1)}
        if "price" not in header:
            return context, []
        context["c4"] = ColumnProfile("dedicated_context", 0.95, category="currency")
        context["c5"] = ColumnProfile("dedicated_context", 0.9, category="other")
        context["c2"].client = "c4"
        return context, [Conflict("c3", [("c4", 0.81), ("c5", 0.64)])]  # price claimed by c4 and c5

    monkeypatch.setattr(cli, "classify_columns", fake_columns)
    (tmp_path / "store.csv").write_text("sku;name;price;currency;note\n001;Tee;9,90;EUR;x\n")
    (tmp_path / "variants.csv").write_text("sku,color\n001,black\n")
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

    code, out, err = run(capsys, "store", "store.csv", "empty.csv")
    assert code == 1 and "Cannot read" in err and stores() == []  # every file is read before registering

    code, out, err = run(capsys, "store", "store.csv", "variants.csv", "store.csv")  # a repeat is read once
    assert code == 0
    [(first, name, status)] = stores()
    assert (name, status) == (None, 1) and first.startswith("st_")
    assert out.splitlines()[-1] == f"Store {first}"  # the last line is the store
    assert "store.csv: 1 rows · 5 columns" in out and "variants.csv: 1 rows · 2 columns" in out
    assert "currency → currency, for name" in out
    assert "price: context from currency (Jev 81%); also claimed by note (64%)" in out  # no question asked
    assert "Jev: classifying column context" in err  # progress goes to stderr

    code, out, err = run(capsys, "store", "variants.csv", "-n", "Orbitrow shop")
    second = stores()[-1][0]
    assert code == 0 and out.splitlines()[-1] == f"Store {second} (Orbitrow shop)"

    code, out, err = run(capsys, "incoming", "supplier.csv")  # the latest store by default
    assert code == 0 and f"incoming for store {second} (Orbitrow shop)" in out
    code, out, err = run(capsys, "incoming", "supplier.csv", "--store", first)
    assert code == 0 and f"incoming for store {first}" in out
    assert run(capsys, "incoming", "missing.csv")[2].startswith("File not found")

    code, out, err = run(capsys, "message", "raise", "prices", "by", "5%", "-s", "Orbitrow shop")
    assert code == 0 and "isn't built yet" in out and second in out
    code, out, err = run(capsys, "message", "hi", "--store", "nope")
    assert code == 1 and "No ready store named nope" in err
    assert len(stores()) == 2  # incoming and message never create a store


def test_same_name_needs_the_id(env, capsys):
    run(capsys, "store", "variants.csv", "-n", "shop")
    run(capsys, "store", "variants.csv", "-n", "shop")
    code, out, err = run(capsys, "message", "hi", "-s", "shop")
    assert code == 1 and "Several stores are named shop" in err


def test_store_needs_files(env, capsys):
    with pytest.raises(SystemExit) as exit:
        cli.main(["store"])
    assert exit.value.code == 2


def test_failed_import_marks_the_store_failed(env, capsys, monkeypatch):
    async def broken(jev, header, rows, step=lambda text: None):
        raise TypeSafeError("down")

    monkeypatch.setattr(cli, "classify_columns", broken)
    code, out, err = run(capsys, "store", "store.csv")
    assert code == 1 and "Jev couldn't answer" in err
    assert [s for _, _, s in stores()] == [-1]
    assert "No store yet" in run(capsys, "message", "hi")[2]  # a failed store is never picked


def test_crash_shows_message_not_traceback(env, capsys, monkeypatch):
    monkeypatch.setattr(cli, "load_csv", lambda path: 1 / 0)
    code, out, err = run(capsys, "store", "store.csv")
    assert code == 1 and "unexpected error" in err and "Traceback" not in err + out
    [log] = (env / "state" / "orbitrows").glob("crash-*.log")
    assert "ZeroDivisionError" in log.read_text() and "Traceback" in log.read_text()
