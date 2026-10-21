"""The `orbitrows` command: store, answer, incoming, message. One process per command; the registry links them."""

import argparse
import asyncio
import contextlib
import csv
import hashlib
import json
import os
import signal
import sys
from pathlib import Path

from typesafe_sdk import TypeSafeError

from orbitrows.pipeline.store import run
from orbitrows.services.cli.crash import report
from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import Recorded, connect, settle
from orbitrows.services.storage import registry
from orbitrows.services.storage import store as storage
from orbitrows.sql.ddl import sql_name

SHOWN = 5                                                     # differences listed when a store would lose data
PAUSED = 3                                                    # exit code: the import waits for the user's answers
NOTICE = ("file", "step", "column", "problem", "probability")  # a notice's fields in the questions JSON


class Refusal(Exception):
    """A request we can't do; its text is shown as is and the command exits with 1."""


def need(variable: str) -> None:
    if not os.environ.get(variable):
        raise Refusal(f"{variable} is not set. Add it to .env and run with: uv run --env-file .env orbitrows …")


def need_key() -> None:
    if (os.environ.get("MODEL") or "jev") == "jev":           # any other MODEL is a stub class path
        need("TYPESAFE_API_KEY")


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


def asked(store_id: str) -> bool:
    """Whether the store's import has asked questions (open or answered)."""
    return (storage.folder(store_id) / "import.json").is_file() and any(storage.load_import(store_id)[k]
                                                                        for k in ("open", "answered"))


def resolve(ref: str | None, status: int = registry.READY) -> tuple[str, str | None]:
    """--store (id or name), or the latest store with this status: ready, or waiting for answers. Waiting includes
    a store left importing after its questions were asked (a killed resume): answer resumes it."""
    if status == registry.WAITING:
        found = [s for s in registry.find(ref, registry.WAITING, registry.IMPORTING) if asked(s[0])]
    else:
        found = registry.find(ref, status)
    found = found if ref else found[:1]
    if not found and status == registry.WAITING:
        raise Refusal(f"No store named {ref} is waiting for answers." if ref else "No store is waiting for answers.")
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
            where = d["header"] if d["header"] is not None else "beyond the header"
            return f'{d["file"]}, row {d["row"] + 1}, {where}: "{d["uploaded"]}" would come back as "{d["rebuilt"]}"'
        return f'{d.get("file") or d.get("table")}: {d["problem"]}'
    more = [f"… and {len(diffs) - SHOWN} more"] if len(diffs) > SHOWN else []
    return "\n  ".join(["The store would lose data, so it wasn't saved:"] + [line(d) for d in diffs[:SHOWN]] + more)


def warning(w: dict) -> str:
    where = " · ".join(str(x) for x in (w["file"], w["step"], w.get("column")) if x)
    p = f" ({w['probability']:.0%})" if isinstance(w.get("probability"), float) else ""
    return f"Warning: {where}: {w['problem']}{p}"


def unseen(progress: dict, warnings: list[dict]) -> list[dict]:
    """The warnings not shown yet, now recorded as shown (each by a short hash of its JSON)."""
    new = []
    for w in warnings:
        key = hashlib.sha256(json.dumps(w, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:8]
        if key not in progress["shown"]:
            progress["shown"].append(key)
            new.append(w)
    return new


def paused(store: tuple[str, str | None], progress: dict, notices: list[dict]) -> int:
    """The open questions and the new notices as one JSON object on stdout, for the LLM driving this CLI."""
    def question(q):
        ask = q["question"]
        options = [{"answer": o["answer"], "probability": o["probability"]} for o in ask.get("options", [])]
        return ({k: q.get(k) for k in ("id", "file", "step", "column", "problem")} | {"type": ask["type"]}
                | ({"options": options} if ask["type"] == "choice" else {}))   # a yes/no is answered yes or no
    print(json.dumps({"store": store[0], "name": store[1], "step": progress["step"],
                      "questions": [question(q) for q in progress["open"]],
                      "notices": [{k: n[k] for k in NOTICE if n.get(k) is not None} for n in notices]},
                     ensure_ascii=False))
    return PAUSED


def importing(store: tuple[str, str | None], progress: dict) -> int:
    """classify on the store's copies, replaying every recorded answer, then pause on its questions or write the
    store. On a failure, a store whose questions were asked waits again (answers kept); any other one fails."""
    store_id = store[0]
    paths = [storage.folder(store_id) / "files" / f for f in progress["files"]]
    save = lambda: storage.save_import(store_id, progress)   # after each new model answer too: a crash keeps them
    step = lambda text: print(f"  {text}…", file=sys.stderr)

    async def ask_model():
        async with Recorded(connect(), progress["calls"], saved=save) as model:
            return await run.classify(model, paths, step=step)

    try:
        registry.set_status(store_id, registry.IMPORTING)
        try:
            with contextlib.redirect_stdout(sys.stderr):      # GLiNER2 prints its config on load: stdout stays JSON
                saved = asyncio.run(ask_model())              # only the model is awaited; Ctrl+C reaches the rest too
        except run.Paused as p:
            progress |= {"step": p.phase, "open": p.questions}
            notices = unseen(progress, p.notices)
            save()
            registry.set_status(store_id, registry.WAITING)
            return paused(store, progress, notices)
        rows = run.rows(paths, saved)
        built = run.build(saved)
        for w in unseen(progress, built["warnings"]):        # before writing, so a refusal shows them too
            print(warning(w))
        save()
        written = storage.write(store_id, built["store"], rows)
        for t in written:
            print(f"{t['table']}: {t['rows']} rows · {t['columns']} columns, from {t['file']}")
        registry.set_status(store_id, registry.READY)         # last: an interrupted import is never left ready
    except storage.Lost as e:                                 # no answer changes the files: failed for good
        registry.set_status(store_id, registry.FAILED)
        raise Refusal(lost(e.diffs)) from None
    except BaseException:  # Jev failing, Ctrl+C/SIGTERM, a crash: never left importing; one asked keeps its answers
        asked = progress["open"] or progress["answered"]
        registry.set_status(store_id, registry.WAITING if asked else registry.FAILED)
        raise
    print(f"Store {label(store)}")
    return 0


def store(paths: list[Path], name: str | None) -> int:
    # Every file is checked first, so a bad one refuses the store before anything is registered.
    unique = {}
    for p in paths:                                           # the same file twice is read once
        unique.setdefault(p.resolve(), p)
    paths = list(unique.values())
    for path in paths:
        read(path)
    tables = {}
    for p in paths:
        tables.setdefault(sql_name(p.stem), []).append(p)
    for table, files in tables.items():
        if len(files) > 1:                                    # order-lines.csv and order_lines.csv are one table
            raise Refusal(f"{', '.join(str(f) for f in files)} would all be the table {table}. Rename one of them.")
    need_key()
    store_id = registry.create(name)
    progress = {"files": [p.name for p in paths], "calls": {}, "open": [], "answered": [], "shown": []}
    try:                                                      # the import runs on the copies: a resume needs no path
        storage.copy_files(store_id, paths)
        storage.save_import(store_id, progress)
    except BaseException:
        registry.set_status(store_id, registry.FAILED)
        raise
    return importing((store_id, name), progress)


def answer(question: str | None, given: str | None, ref: str | None) -> int:
    """Records the user's answer to an open question; once none is open, the import runs again (a retry too)."""
    store_id = resolve(ref, registry.WAITING)[0]
    with storage.locked(store_id):                            # to the end: a parallel answer waits, so none is lost
        return record(resolve(store_id, registry.WAITING), question, given)   # again: the one it waited for may be done


def record(target: tuple[str, str | None], question: str | None, given: str | None) -> int:
    progress = storage.load_import(target[0])
    if question is not None:
        q = next((q for q in progress["open"] if q["id"] == question), None)
        if given is None or q is None:
            ids = ", ".join(q["id"] for q in progress["open"])
            raise Refusal(f"Give an open question's id and its answer. Open questions of {label(target)}: {ids}" if ids
                          else f"Store {label(target)} has no open question. Retry its import: orbitrows answer -s "
                               f"{target[0]}")
        ask = q["question"]
        valid = ({str(o["answer"]): o["value"] for o in ask["options"]} if ask["type"] == "choice"
                 else {"yes": "yes", "no": "no"})
        match = next((a for a in valid if a.casefold() == given.casefold()), None)
        if match is None:
            raise Refusal(f"{given} isn't an answer to {question}. Answers: {', '.join(valid)}")
        settle(progress["calls"], ask["call"], ask["key"], valid[match])
        progress["open"].remove(q)
        progress["answered"].append({k: q.get(k) for k in ("id", "file", "step", "column", "problem")}
                                    | {"answer": match})
        storage.save_import(target[0], progress)
    if progress["open"]:
        registry.set_status(target[0], registry.WAITING)      # a killed resume's store was left importing
        return paused(target, progress, [])
    need_key()
    return importing(target, progress)


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
    s = commands.add_parser("store", help="create a store from its CSV files; prints its id, or its questions")
    s.add_argument("files", nargs="+", type=Path, metavar="csv")
    s.add_argument("-n", "--name", help="a name for the store (optional)")
    a = commands.add_parser("answer", help="answer a question of an import waiting for answers; then it resumes")
    a.add_argument("question", nargs="?", help="the question's id, from the questions JSON")
    a.add_argument("answer", nargs="?", help="one of its options' answers, or yes/no")
    a.add_argument("-s", "--store", help="store id or name (default: the latest store waiting for answers)")
    i = commands.add_parser("incoming", help="add a supplier file to a store")
    i.add_argument("file", type=Path, metavar="csv")
    m = commands.add_parser("message", help="send a request about the store's data")
    m.add_argument("text", nargs="+")
    for sub in (i, m):
        sub.add_argument("-s", "--store", help="store id or name (default: the latest store)")
    return cli


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # a kill is a Ctrl+C: the store is never left importing
    args = parser().parse_args(argv)
    try:
        match args.command:
            case "store":
                return store(args.files, args.name)
            case "answer":
                return answer(args.question, args.answer, args.store)
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
