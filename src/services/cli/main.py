"""The `orbitrows` command: store, incoming, message. One process per command; the registry links them."""

import argparse
import asyncio
import csv
import os
import sys
from pathlib import Path

from typesafe_sdk import TypeSafeError

from orbitrows.pipeline.store import run
from orbitrows.services.cli.crash import report
from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import connect
from orbitrows.services.storage import registry
from orbitrows.services.storage import store as storage

SHOWN = 5                                                     # differences listed when a store would lose data


class Refusal(Exception):
    """A request we can't do; its text is shown as is and the command exits with 1."""


def need(variable: str) -> None:
    if not os.environ.get(variable):
        raise Refusal(f"{variable} is not set. Add it to .env and run with: uv run --env-file .env orbitrows …")


def read(path: Path) -> dict:
    """The file as read_table reads it (Sniffer's header guess), or a refusal."""
    if not path.is_file():
        raise Refusal(f"File not found: {path}")
    try:
        return read_table(path)
    except (ValueError, csv.Error, OSError) as e:
        raise Refusal(f"Cannot read {path.name}: {e}") from None
    except IndexError:                                        # not one non-empty row
        raise Refusal(f"Cannot read {path.name}: empty file") from None


def resolve(ref: str | None) -> tuple[str, str | None]:
    """--store (id or name), or the latest ready store."""
    found = registry.find(ref)
    if not found:
        raise Refusal(f"No ready store named {ref}." if ref else "No store yet. Create one: orbitrows store <csv files…>")
    if len(found) > 1:
        raise Refusal(f"Several stores are named {ref}: {', '.join(i for i, _ in found)}. Use the id.")
    return found[0]


def label(store: tuple[str, str | None]) -> str:
    store_id, name = store
    return f"{store_id} ({name})" if name else store_id


def lost(diffs: list[dict]) -> str:
    """The refusal when the store's tables would not give the uploaded files back."""
    def line(d):
        if "header" in d:
            return f'{d["file"]}, row {d["row"] + 1}, {d["header"]}: "{d["uploaded"]}" would come back as "{d["rebuilt"]}"'
        return f'{d.get("file") or d.get("table")}: {d["problem"]}'
    more = [f"… and {len(diffs) - SHOWN} more"] if len(diffs) > SHOWN else []
    return "\n  ".join(["The store would lose data, so it wasn't saved:"] + [line(d) for d in diffs[:SHOWN]] + more)


def warning(w: dict) -> str:
    where = " · ".join(str(x) for x in (w["file"], w["step"], w.get("column")) if x)
    p = f" ({w['probability']:.0%})" if isinstance(w.get("probability"), float) else ""
    return f"Warning: {where}: {w['problem']}{p}"


async def store(paths: list[Path], name: str | None) -> None:
    # Every file is checked first, so a bad one refuses the store before anything is registered.
    unique = {}
    for p in paths:                                           # the same file twice is read once
        unique.setdefault(p.resolve(), p)
    paths = list(unique.values())
    for path in paths:
        read(path)
    names = [p.name for p in paths]
    if repeated := sorted({n for n in names if names.count(n) > 1}):
        raise Refusal(f"Several files are named {', '.join(repeated)}; a store keeps its files by name. Rename them.")
    if not os.environ.get("MODEL"):                           # MODEL picks another model (a test stub)
        need("TYPESAFE_API_KEY")
    store_id = registry.create(name)
    try:
        storage.copy_files(store_id, paths)
        step = lambda text: print(f"  {text}…", file=sys.stderr)
        async with connect() as model:
            saved = await run.classify(model, paths, step=step)
        rows = run.rows(paths, saved)
        built = run.build(saved, rows)
        for w in built["warnings"]:                           # before writing, so a refusal shows them too
            print(warning(w))
        if diffs := [d for d in built["diffs"] if d["kind"] == "value"]:
            raise Refusal(lost(diffs))
        try:
            tables = storage.write(store_id, built["store"], rows)
        except storage.Lost as e:
            raise Refusal(lost(e.diffs)) from None
    except BaseException:  # Jev failing, Ctrl+C, a crash, data lost: the store is marked failed, never left importing
        registry.set_status(store_id, registry.FAILED)
        raise
    registry.set_status(store_id, registry.READY)
    for t in tables:
        print(f"{t['table']}: {t['rows']} rows · {t['columns']} columns, from {t['file']}")
    print(f"Store {label((store_id, name))}")


def incoming(path: Path, ref: str | None) -> None:
    target = resolve(ref)
    table = read(path)
    # ponytail: not saved anywhere yet; the file is only checked
    print(f"{path.name}: {len(table['rows'])} rows · {len(table['header'])} columns, incoming for store {label(target)}")
    print("Incoming files aren't saved yet, so nothing else uses it.")


def message(text: str, ref: str | None) -> None:
    target = resolve(ref)
    # ponytail: the prompt pipeline plugs in here
    print(f"Message for store {label(target)}: running messages isn't built yet, so nothing was changed.")


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="orbitrows", description="Reconcile a store's CSV export with supplier files.")
    commands = cli.add_subparsers(dest="command", required=True)
    s = commands.add_parser("store", help="create a store from its CSV files; prints its id")
    s.add_argument("files", nargs="+", type=Path, metavar="csv")
    s.add_argument("-n", "--name", help="a name for the store (optional)")
    i = commands.add_parser("incoming", help="add a supplier file to a store")
    i.add_argument("file", type=Path, metavar="csv")
    m = commands.add_parser("message", help="send a request about the store's data")
    m.add_argument("text", nargs="+")
    for sub in (i, m):
        sub.add_argument("-s", "--store", help="store id or name (default: the latest store)")
    return cli


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        match args.command:
            case "store":
                asyncio.run(store(args.files, args.name))
            case "incoming":
                incoming(args.file, args.store)
            case "message":
                message(" ".join(args.text), args.store)
    except Refusal as e:
        print(e, file=sys.stderr)
        return 1
    except TypeSafeError as e:
        print(f"Jev couldn't answer ({type(e).__name__}). Try again.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(report(e), file=sys.stderr)
        return 1
    return 0
