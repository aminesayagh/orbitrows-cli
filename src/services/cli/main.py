"""The `orbitrows` command: store, incoming, message. One process per command; the registry links them."""

import argparse
import asyncio
import os
import sys
from pathlib import Path

from typesafe_sdk import TypeSafeError

from orbitrows.pipeline.context import classify_columns
from orbitrows.services.cli.crash import report
from orbitrows.services.csv.reader import load_csv
from orbitrows.services.jev.client import connect
from orbitrows.services.storage import registry


class Refusal(Exception):
    """A request we can't do; its text is shown as is and the command exits with 1."""


def need(variable: str) -> None:
    if not os.environ.get(variable):
        raise Refusal(f"{variable} is not set. Add it to .env and run with: uv run --env-file .env orbitrows …")


def read(path: Path) -> tuple[list[str], list[list[str]]]:
    if not path.is_file():
        raise Refusal(f"File not found: {path}")
    try:
        return load_csv(path)
    except ValueError as e:
        raise Refusal(f"Cannot read {e}") from None


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


def context_lines(header: list[str], context: dict) -> list[str]:
    """One line per column that carries or declares a unit, currency or language."""
    name = lambda key: header[int(key[1:]) - 1] or f"column {key[1:]}"
    lines = []
    for key, c in context.items():
        if c.role in ("dedicated_context", "both") and c.category:
            described = [name(k) for k, other in context.items() if other.client == key]
            unit = f" {c.header_context}" if c.header_context else ""
            lines.append(f"{name(key)} → {c.category}{unit}" + (f", for {', '.join(described)}" if described else ""))
        elif c.role == "header_context" and c.header_context:
            lines.append(f"{name(key)} → {c.category} {c.header_context}")
    return lines


async def profile(header: list[str], rows: list[list[str]]) -> list[str]:
    """Jev's column profile, without questions: a conflict takes Jev's top pick and says so."""
    step = lambda text: print(f"  {text}…", file=sys.stderr)
    context, conflicts = await classify_columns(connect(), header, rows, step=step)
    name = lambda key: header[int(key[1:]) - 1] or f"column {key[1:]}"
    notes = []
    for conflict in conflicts:
        (pick, p), *others = conflict.options
        context[conflict.key].client = pick
        alternatives = ", ".join(f"{name(k)} ({q:.0%})" for k, q in others)
        notes.append(f"{name(conflict.key)}: context from {name(pick)} (Jev {p:.0%}); also claimed by {alternatives}")
    return context_lines(header, context) + notes


async def store(paths: list[Path], name: str | None) -> None:
    # Every file is read first, so a bad one refuses the store before anything is registered.
    tables = [(path, *read(path)) for path in dict.fromkeys(paths)]
    need("OPENROUTER_API_KEY")
    store_id = registry.create(name)
    try:
        for path, header, rows in tables:
            print(f"{path.name}: {len(rows)} rows · {len(header)} columns")
            for line in await profile(header, rows) or ["no unit, currency or language columns"]:
                print(f"  {line}")
    except BaseException:  # Jev failing, Ctrl+C, a crash: the store is marked failed, never left importing
        registry.set_status(store_id, registry.FAILED)
        raise
    registry.set_status(store_id, registry.READY)
    print(f"Store {label((store_id, name))}")


def incoming(path: Path, ref: str | None) -> None:
    target = resolve(ref)
    header, rows = read(path)
    # ponytail: not saved anywhere yet; the file is only checked
    print(f"{path.name}: {len(rows)} rows · {len(header)} columns, incoming for store {label(target)}")
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
