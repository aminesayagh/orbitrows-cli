"""The `orbitrows` command: store, answer, incoming, message, undo, exit, export. One process per command; the registry links
them, and a store's session server holds its messages' changes until exit."""

import argparse
import asyncio
import contextlib
import csv
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import sys
from pathlib import Path

from typesafe_sdk import TypeSafeError

from orbitrows.pipeline.store import run
from orbitrows.pipeline import incoming as incoming_pipeline
from orbitrows.services.cli.crash import report
from orbitrows.services.csv.reader import EncodingNeeded, read_table
from orbitrows.services import questions, ready
from orbitrows.services.model.client import Recorded, connect
from orbitrows.services.session import client as session
from orbitrows.services.storage import export as exporting
from orbitrows.services.storage import incoming as incoming_storage
from orbitrows.services.storage import registry
from orbitrows.services.storage import store as storage
from orbitrows.sql.ddl import sql_name

SHOWN = 5                                                     # differences listed when a store would lose data
PAUSED = 3                                                    # exit code: the import or message waits for answers
NOTICE = ("file", "step", "column", "problem", "probability")  # a notice's fields in the questions JSON
QUESTION = ("id", "file", "step", "column", "problem")      # a question's, each one shown (null when missing)


class Refusal(Exception):
    """A request we can't do; its text is shown as is and the command exits with 1."""


def need(variable: str) -> None:
    if not os.environ.get(variable):
        raise Refusal(f"{variable} is not set. Add it to .env and run with: uv run --env-file .env orbitrows …")


def need_key() -> None:
    if (os.environ.get("MODEL") or "jev") == "jev":           # any other MODEL is a stub class path
        need("TYPESAFE_API_KEY")


def read(path: Path, encoding: str | None = None) -> dict:
    """The file as read_table reads it (Sniffer's header guess), or a refusal."""
    if not path.is_file():
        raise Refusal(f"File not found: {path}")
    try:
        table = read_table(path, encoding=encoding) if encoding is not None else read_table(path)
    except EncodingNeeded:
        raise
    except (ValueError, csv.Error, OSError) as e:
        raise Refusal(f"Cannot read {path.name}: {e}") from None
    except IndexError:                                        # not one non-empty row
        raise Refusal(f"Cannot read {path.name}: empty file") from None
    if not table["rows"]:
        raise Refusal(f"Cannot read {path.name}: no data rows")
    return table


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
    notices = [{k: n[k] for k in NOTICE if n.get(k) is not None} for n in notices]
    print(json.dumps({"store": store[0], "name": store[1]} | questions.asking(progress, notices, QUESTION, nulls=True),
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
            kwargs = {"encodings": progress["encodings"]} if progress.get("encodings") else {}
            return await run.classify(model, paths, step=step, names=progress.get("names", {}), **kwargs)

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
        rows = run.rows(paths, saved, progress.get("encodings"))
        built = run.build(saved, progress.get("names", {}))
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


def store(paths: list[Path], name: str | None, encoding: str | None = None) -> int:
    # Check certain files before registration; ambiguous ones are validated after the user's answer.
    unique = {}
    for p in paths:                                           # the same file twice is read once
        unique.setdefault(p.resolve(), p)
    paths = list(unique.values())
    uncertain, encodings = [], {}
    for path in paths:
        try:
            table = read(path, encoding)
            if encoding is not None:
                encodings[path.name] = table["encoding"]
        except EncodingNeeded as e:
            if not e.options:
                raise Refusal(f"Cannot read {path.name}: no usable text encoding found. Try --encoding.") from None
            question_id = "q_" + hashlib.sha256(path.name.encode()).hexdigest()[:8]
            uncertain.append({"id": question_id, "file": path.name, "step": "encoding", "column": None,
                              "problem": "This file's encoding isn't certain. Which reading is right?",
                              "question": {"type": "choice", "options": [o | {"value": o["answer"]}
                                                               for o in e.options]}})
    tables = {}
    for p in paths:
        tables.setdefault(sql_name(p.stem), []).append(p)
    for table, files in tables.items():
        if len(files) > 1:                                    # order-lines.csv and order_lines.csv are one table
            raise Refusal(f"{', '.join(str(f) for f in files)} would all be the table {table}. Rename one of them.")
    if not uncertain:
        need_key()
    store_id = registry.create(name)
    progress = {"files": [p.name for p in paths], "calls": {}, "names": {}, "encodings": encodings,
                "open": uncertain, "answered": [], "shown": [], "step": "encoding" if uncertain else None}
    try:                                                      # the import runs on the copies: a resume needs no path
        storage.copy_files(store_id, paths)
        storage.save_import(store_id, progress)
    except BaseException:
        registry.set_status(store_id, registry.FAILED)
        raise
    if uncertain:
        registry.set_status(store_id, registry.WAITING)
        return paused((store_id, name), progress, [])
    return importing((store_id, name), progress)


def answer(question: str | None, given: str | None, ref: str | None) -> int:
    """Records the user's answer to an open question; once none is open, the import runs again (a retry too). A
    message's question (m_…) is its session's."""
    if question and question.startswith("m_"):
        return answered(question, given, ref)
    if question and question.startswith("i_"):
        return answer_incoming(question, given, ref)
    store_id = resolve(ref, registry.WAITING)[0]
    with storage.locked(store_id):                            # to the end: a parallel answer waits, so none is lost
        return record(resolve(store_id, registry.WAITING), question, given)   # again: the one it waited for may be done


def record(target: tuple[str, str | None], question: str | None, given: str | None) -> int:
    progress = storage.load_import(target[0])
    if question is not None:
        try:
            q = questions.find(progress, question, given, f" of {label(target)}",
                               f"Store {label(target)} has no open question. Retry its import: orbitrows answer -s "
                               f"{target[0]}")
            match = questions.match(q, given)
        except questions.Invalid as e:
            raise Refusal(str(e)) from None
        if q["question"]["type"] == "name":                   # a table split out of a file: kept for the replay
            names = progress.setdefault("names", {})
            taken = ({sql_name(Path(f).stem, default="imported") for f in progress["files"]}
                     | {n for i, n in names.items() if i != question}
                     | {o["question"]["default"] for o in progress["open"] if o is not q and o["question"]["type"] == "name"})
            if match in taken:
                raise Refusal(f"{given} can't name this table: {match} is another table's name. Give another name, "
                              "or skip.")
            names[question] = match
        if q["step"] == "encoding":
            file = q["file"]
            table = read(storage.folder(target[0]) / "files" / file, match)
            progress.setdefault("encodings", {})[file] = table["encoding"]
        questions.record(progress, q, match, QUESTION, nulls=True)
        storage.save_import(target[0], progress)
    if progress["open"]:
        registry.set_status(target[0], registry.WAITING)      # a killed resume's store was left importing
        return paused(target, progress, [])
    need_key()
    return importing(target, progress)


def incoming_paused(target: tuple[str, str | None], upload_id: str, progress: dict, notices: list) -> int:
    notices = [{k: n[k] for k in NOTICE if n.get(k) is not None} for n in notices]   # as paused() shows them
    print(json.dumps({"store": target[0], "name": target[1], "upload": upload_id}
                     | questions.asking(progress, notices, QUESTION, nulls=True), ensure_ascii=False))
    return PAUSED


def inspect_incoming(path: Path, encoding: str | None = None) -> None:
    """Check readability and row widths before any model call, without guessing whether row 1 is a header."""
    if not path.is_file():
        raise Refusal(f"File not found: {path}")
    try:
        rows = read_table(path, has_header=False, encoding=encoding)["rows"]
    except EncodingNeeded:
        raise
    except (ValueError, csv.Error, OSError) as e:
        raise Refusal(f"Cannot read {path.name}: {e}") from None
    except IndexError:
        raise Refusal(f"Cannot read {path.name}: empty file") from None
    if not rows:
        raise Refusal(f"Cannot read {path.name}: empty file")
    for number, row in enumerate(rows, 1):
        if len(row) != len(rows[0]):
            raise Refusal(f"Cannot read {path.name}: row {number} has {len(row)} cells; expected {len(rows[0])}.")


def resume_incoming(target: tuple[str, str | None], upload_id: str, progress: dict) -> int:
    try:
        return classify_incoming(target, upload_id, progress)
    except Refusal:                                           # terminal: nobody will resume its CSV copy
        shutil.rmtree(incoming_storage.pending_dir(target[0], upload_id), ignore_errors=True)
        raise


def classify_incoming(target: tuple[str, str | None], upload_id: str, progress: dict) -> int:
    store_id = target[0]
    path = incoming_storage.pending_path(store_id, upload_id, progress)
    save = lambda: incoming_storage.save_pending(store_id, upload_id, progress)
    if "catalog" not in progress:                             # the store as committed when the upload started: kept,
        progress["catalog"] = incoming_pipeline.incoming_match.catalog(store_id)   # so a resume's calls are the same
        save()

    async def classify():
        async with Recorded(connect(), progress["calls"], saved=save) as model:
            return await incoming_pipeline.classify(model, path, progress["encoding"], progress["catalog"])

    try:
        with contextlib.redirect_stdout(sys.stderr):          # GLiNER2 prints its config on load: stdout stays JSON
            result = asyncio.run(classify())
    except run.Paused as p:
        token = upload_id.removeprefix("up_")
        progress["open"] = [q | {"id": f"i_{token}_{q['id'].removeprefix('q_')}"} for q in p.questions]
        progress["step"] = p.phase
        notices = unseen(progress, p.notices)
        save()
        return incoming_paused(target, upload_id, progress, notices)
    table = result["table"]
    if not table["rows"]:
        raise Refusal(f"Cannot read {progress['file']}: no data rows")
    for notice in unseen(progress, result["notices"]):
        print(warning(notice))
    save()
    folder = incoming_storage.save_classified(store_id, upload_id, table, result["saved"])
    written = ask(target, {"op": "incoming", "folder": str(folder)})   # the session writes it, started if needed
    print(f"{progress['file']}: {written['rows']} rows · {written['columns']} columns, incoming for store "
          f"{label(target)} as {written['table']}")
    keys = {k["id"]: k | {"sql_type": k["sql_type"].upper()} for k in progress["catalog"]["keys"]}
    for pos, column in (links := result["saved"].get("matching", {}).get("keys", {})).items():
        share = incoming_pipeline.incoming_match.shares(incoming_pipeline.profile.values(table, pos), keys[column])
        several = result["saved"]["profile"]["columns"][pos]["max_repeat"] > 1
        print(f"  key {table['header'][pos]} → {keys[column]['table']}.{keys[column]['header']} "
              f"({share:.0%} of its values)" + " (several rows per record)" * several)
    if not links:
        print("  no key linked to a store key: a message can't match its rows to the store's records")
    print(f"Change {written['n']} made in store {label(target)}: saved by orbitrows exit, reverted by orbitrows undo.",
          file=sys.stderr)
    return 0


def answer_incoming(question: str, given: str | None, ref: str | None) -> int:
    parts = question.split("_", 2)
    if len(parts) != 3 or not parts[1]:
        raise Refusal(f"Invalid incoming question: {question}")
    upload_id = "up_" + parts[1]
    candidates = registry.find(ref, registry.READY)
    found = [(sid, name) for sid, name in candidates
             if (incoming_storage.pending_dir(sid, upload_id) / "progress.json").is_file()]
    if not found:
        raise Refusal(f"No pending incoming upload has question {question}.")
    target = found[0]
    with storage.locked(target[0]):
        progress = incoming_storage.load_pending(target[0], upload_id)
        try:
            asked = questions.find(progress, question, given, f" for {progress['file']}",
                                   f"No open question for incoming upload {upload_id}.")
            matched = questions.match(asked, given)
        except questions.Invalid as e:
            raise Refusal(str(e)) from None
        if asked["step"] == "encoding":
            try:
                inspect_incoming(incoming_storage.pending_path(target[0], upload_id, progress), matched)
            except Refusal:                                   # a bad file: no answer makes it readable
                shutil.rmtree(incoming_storage.pending_dir(target[0], upload_id), ignore_errors=True)
                raise
            progress["encoding"] = matched
        questions.record(progress, asked, matched, QUESTION, nulls=True)
        incoming_storage.save_pending(target[0], upload_id, progress)
        if progress["open"]:
            return incoming_paused(target, upload_id, progress, [])
        need_key()
        return resume_incoming(target, upload_id, progress)


def incoming(path: Path, ref: str | None, encoding: str | None = None) -> int:
    target = resolve(ref)
    try:
        inspect_incoming(path, encoding)
    except EncodingNeeded as e:
        if not e.options:
            raise Refusal(f"Cannot read {path.name}: no usable text encoding found. Try --encoding.") from None
        upload_id, progress = incoming_storage.start(target[0], path)
        token = upload_id.removeprefix("up_")
        progress["step"] = "encoding"
        progress["open"] = [{"id": f"i_{token}_encoding", "file": path.name, "step": "encoding", "column": None,
                             "problem": "This file's encoding isn't certain. Which reading is right?",
                             "question": {"type": "choice", "options": [o | {"value": o["answer"]}
                                                                      for o in e.options]}}]
        incoming_storage.save_pending(target[0], upload_id, progress)
        return incoming_paused(target, upload_id, progress, [])
    need_key()
    upload_id, progress = incoming_storage.start(target[0], path, encoding)
    return resume_incoming(target, upload_id, progress)


def ask(target: tuple[str, str | None], request: dict, launch: bool = True) -> dict | None:
    """The store's session server's reply (None: no server, and not launch); what its last session lost on stderr."""
    try:
        reply = session.ask(target[0], request, launch)
    except session.Unavailable as e:
        raise Refusal(str(e)) from None
    if reply and reply.get("lost"):
        print(f"The last session of store {label(target)} ended without saving (a crash?), so these changes were "
              f"lost. Send them again if they're still wanted (journal: {reply['lost']['file']}):", file=sys.stderr)
        for change in reply["lost"]["changes"]:
            print("\n    ".join([f"  {change['prompt']}"] + change["sql"]), file=sys.stderr)
    if reply and not reply["ok"]:
        raise Refusal(reply["error"])
    return reply


def message(text: str, ref: str | None) -> int:
    """Run by the prompt pipeline in the store's session (started if needed, after the key is checked): applied, not
    saved until exit, undo reverts it; or paused on questions for the user."""
    target, request = resolve(ref), {"op": "message", "text": text}
    if (reply := ask(target, request, launch=False)) is None:
        need_key()                                            # the server runs the model with this process's env
        reply = ask(target, request)
    return replied(target, reply)


def answered(question: str, given: str | None, ref: str | None) -> int:
    """answer for a message's question: sent to the session of --store, else of the latest ready store that has one."""
    if ref:
        target = resolve(ref)
    elif not (target := next((s for s in registry.find() if (storage.folder(s[0]) / "session.sock").exists()), None)):
        raise Refusal("No store has an open session, so no message is waiting for answers.")
    if not (reply := ask(target, {"op": "answer", "id": question, "answer": given}, launch=False)):
        raise Refusal(f"Store {label(target)} has no open session, so no message is waiting for answers.")
    return replied(target, reply)


def replied(target: tuple[str, str | None], reply: dict) -> int:
    """The session's reply to a message or an answer as one JSON object on stdout: its open questions (exit 3), or
    what was applied (a query's rows, a change's row count)."""
    head = {"store": target[0], "name": target[1]}
    if "questions" in reply:
        print(json.dumps(head | {k: reply[k] for k in ("step", "questions", "notices")}, ensure_ascii=False))
        return PAUSED
    print(json.dumps(head | {"applied": reply["ran"], "notices": reply["notices"]}, ensure_ascii=False, default=str))
    if "n" in reply:
        print(f"Change {reply['n']} made in store {label(target)}: saved by orbitrows exit, reverted by orbitrows "
              "undo.", file=sys.stderr)
    return 0


def undo(ref: str | None) -> None:
    target = resolve(ref)
    if not (reply := ask(target, {"op": "undo"}, launch=False)):
        raise Refusal(f"Nothing to undo: store {label(target)} has no open session.")
    print(f"Undone: change {reply['undone']} ({reply['prompt']}).")


def leave(ref: str | None) -> None:
    """exit: the session's changes are committed, then its server stops."""
    target = resolve(ref)
    if not (reply := ask(target, {"op": "exit"}, launch=False)):
        print(f"Nothing to save: store {label(target)} has no open session.")
        return
    if not reply.get("ok"):                                   # the COMMIT failed: nothing is said saved
        raise Refusal(reply["error"])
    print("Saved." + (f" The message waiting for answers ({reply['dropped']}) was dropped." if "dropped" in reply else ""))


def export(folder: Path, force: bool, ref: str | None) -> None:
    """Every uploaded file rebuilt from the store's tables into folder, as one JSON object on stdout: through the
    store's session when one runs (its unsaved changes included), else from session.db, read only."""
    target, folder = resolve(ref), folder.resolve()          # absolute: the session runs in another folder
    path = storage.folder(target[0])
    request = {"op": "export", "folder": str(folder), "force": force}
    if (path / "session.sock").exists() and (reply := ask(target, request, launch=False)) is not None:
        files = reply["files"]
    else:
        with contextlib.closing(sqlite3.connect(f"file:{path / 'session.db'}?mode=ro", uri=True)) as db:
            try:
                files = exporting.export(db, path, folder, force)
            except (FileExistsError, exporting.Unwritable) as e:
                raise Refusal(str(e)) from None
    print(json.dumps({"store": target[0], "name": target[1], "files": files}, ensure_ascii=False))


def parser() -> argparse.ArgumentParser:
    cli = argparse.ArgumentParser(prog="orbitrows", description="Reconcile a store's CSV export with supplier files.")
    commands = cli.add_subparsers(dest="command", required=True)
    s = commands.add_parser("store", help="create a store from its CSV files; prints its id, or its questions")
    s.add_argument("files", nargs="+", type=Path, metavar="csv")
    s.add_argument("-n", "--name", help="a name for the store (optional)")
    s.add_argument("--encoding", help="encoding of all uploaded files, when known (for example cp1252)")
    a = commands.add_parser("answer", help="answer a question of an import or a message; then it resumes")
    a.add_argument("question", nargs="?", help="the question's id, from the questions JSON")
    a.add_argument("answer", nargs="?", help="one of its options' answers, or yes/no; for a table's name, "
                                                     "any name, or skip")
    a.add_argument("-s", "--store", help="store id or name (default: the latest store waiting for answers; for a "
                                         "message's question, m_…, the latest ready store with an open session)")
    i = commands.add_parser("incoming", help="add a supplier file to a store")
    i.add_argument("file", type=Path, metavar="csv")
    i.add_argument("--encoding", help="encoding of this file, when known")
    m = commands.add_parser("message", help="send a request about the store's data")
    m.add_argument("text", nargs="+")
    u = commands.add_parser("undo", help="revert the session's last change")
    e = commands.add_parser("exit", help="save the session's changes and end it")
    commands.add_parser("setup", help="prepare everything a message needs, once: Duckling (dates, in Docker) and "
                                      "the models (about 2 GB the first time)")
    x = commands.add_parser("export", help="write the store's files, rebuilt from its tables, into a folder")
    x.add_argument("-o", "--output", type=Path, required=True, metavar="folder")
    x.add_argument("--force", action="store_true", help="replace files already in the folder")
    for sub in (i, m, u, e, x):
        sub.add_argument("-s", "--store", help="store id or name (default: the latest store)")
    return cli


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGTERM, signal.default_int_handler)  # a kill is a Ctrl+C: the store is never left importing
    args = parser().parse_args(argv)
    try:
        match args.command:
            case "store":
                return store(args.files, args.name, args.encoding)
            case "answer":
                return answer(args.question, args.answer, args.store)
            case "incoming":
                return incoming(args.file, args.store, args.encoding)
            case "message":
                return message(" ".join(args.text), args.store)
            case "undo":
                undo(args.store)
            case "exit":
                leave(args.store)
            case "export":
                export(args.output, args.force, args.store)
            case "setup":
                if failed := ready.setup():
                    raise Refusal("Not ready: " + "; ".join(failed))
                print("Ready.")
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
