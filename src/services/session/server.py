"""A store's session server: `python -m orbitrows.services.session <store_id> [--owner <sid>]`, started detached by
the CLI. It holds one transaction open on the store's session.db: each change runs in its own SAVEPOINT (undo rolls
back to it), and the whole session is committed when it ends: `orbitrows exit`, the owner's terminal closed (--owner,
the terminal's session leader), ORBITROWS_IDLE seconds without a request, SIGTERM or SIGHUP. A crash loses the
session: the journal says what was lost.

A message runs the prompt pipeline (prompt()). One that pauses on questions waits in memory (one at a time, a new
message replaces it) until each is answered (op "answer", ids m_…), then runs again, its model answers replayed.
An incoming upload, classified by the CLI, is written as a change too (op "incoming"): its CSV stays in its pending
folder until the COMMIT moves it under incoming/, and an undo removes that folder.

    session.sock   the Unix socket: one JSON request line in, one JSON reply line out, one client at a time
    journal.jsonl  each change applied ({"n", "prompt", "sql", "at"}) or undone ({"undo": n}), fsynced; removed by a
                   successful COMMIT. A next server finding it kept it as journal-lost-<time>.jsonl and says so
    session.lock   flock'd while a server runs: one server per store, and a new one waits for the last one's COMMIT
    session.log    the server's output
"""
import argparse
import asyncio
import fcntl
import importlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

from orbitrows.pipeline.store.profile import refresh
from orbitrows.services import duckling, questions, ready
from orbitrows.services.storage import registry

IDLE = 1800                                                   # seconds without a request before it saves and ends
TICK = 1                                                      # seconds between checks of the owner, idle time, signals
READ = 30                                                     # seconds a client has to send its request line
WAIT = 10                                                     # seconds a new server waits for the last one to end
ROWS = 1000                                                   # rows of a statement sent back; "more" says there were others
NAME = 60                                                     # characters of the prompt kept as the change's name
QUESTION = ("id", "request", "step", "problem")              # a question's fields shown, those it has
# a statement that changes the tables' structure: its first keyword, after blanks and comments (one statement each:
# sqlite3 runs no more); refused for now (user, 2026-10-21)
STRUCTURE = re.compile(r"(?:\s|--[^\n]*|/\*.*?\*/)*(?:CREATE|ALTER|DROP)\b", re.IGNORECASE | re.DOTALL)
PASSED = re.compile(r":(rows_\w+)")                         # a later request's ids, passed as it runs
RETURNING = re.compile(r'RETURNING "[^"]+"$')
WRITES = re.compile(r'\s*(?:UPDATE|INSERT\s+INTO|DELETE\s+FROM)\s+"([^"]+)"', re.IGNORECASE)   # the table it changes


def folder(store_id: str):
    return registry.home() / "stores" / store_id              # storage.folder, without its pipeline imports


def prompt(db, text: str, calls: dict) -> dict:
    """The prompt pipeline on the session's connection (so on its uncommitted changes), each model answer kept in
    `calls` and replayed from there when the message runs again after its questions are answered. → {"program",
    "errors", "notices"}, or {"paused": {"step", "questions", "notices"}}. Its progress goes to the log."""
    from orbitrows.pipeline.prompt import pipeline            # here: ~0.6 s of imports a stub handler doesn't need
    from orbitrows.pipeline.store.run import Paused
    from orbitrows.services.model.client import Recorded, connect

    ready.wait()                                              # the session's preload: never a model loaded twice

    async def run():
        async with Recorded(connect(), calls) as model:      # the model's client opens on its first call not recorded
            return await pipeline.run(text, model, db, log)
    try:
        out = asyncio.run(run())
    except Paused as p:
        return {"paused": {"step": p.phase, "questions": p.questions, "notices": p.notices}}
    a = out["assembly"]
    unsure = {w.get("request") for w in out.get("conditions", {}).get("warnings", [])}
    return {"program": a["program"], "errors": a["errors"],   # assembly's questions list its errors too: told apart
            "notices": [q for q in a["questions"] if "question" not in q and "error" not in q],
            # every row changed while the rows' step was unsure or failed: the user confirms first (user, 2026-10-21)
            "confirm": [q for q in a["questions"] if q.get("scope") == "all_rows" and q.get("request") in unsure]}


def handler():
    """ORBITROWS_HANDLER (`module:function`, a test's), else prompt(): handler(db, text, calls) -> {"program":
    [{"request", "ast", "sql": [statements]}], "errors"?, "notices"?} or {"paused": …}, as prompt()'s."""
    if not (spec := os.environ.get("ORBITROWS_HANDLER")):
        return prompt
    module, _, name = spec.partition(":")
    return getattr(importlib.import_module(module), name)


def asking(p: dict, notices: list) -> dict:
    """The reply of a message paused on its open questions."""
    return {"ok": True} | questions.asking(p, notices, QUESTION, nulls=False)


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:                                   # someone else's process: alive
        pass
    return True


def log(text: str) -> None:
    print(f"{registry.now()} {text}", flush=True)


class Session:
    """session.db's open transaction, its changes (savepoints c<n>) and the socket clients reach it on."""

    def __init__(self, store_id: str, handle):
        self.dir = folder(store_id)
        self.journal = self.dir / "journal.jsonl"
        self.socket = self.dir / "session.sock"
        self.handle = handle
        self.failed = None                                    # why the COMMIT failed, once ended
        self.changes = []                                     # [(n, prompt)], each one's savepoint open, oldest first
        self.pending = None                                   # the message waiting for answers: {"prompt", "calls",
                                                              # "open", "answered", "step", "shown"}, in memory only
        self.count = 0                                        # the last change's n
        self.uploads = {}                                     # {n: (pending folder, source_file)}: placed on COMMIT
        self.db = sqlite3.connect(f"file:{self.dir / 'session.db'}?mode=rw", uri=True, isolation_level=None)
        for pragma in ("foreign_keys = ON", "synchronous = FULL", "journal_mode = DELETE"):   # per connection
            self.db.execute(f"PRAGMA {pragma}")
        self.db.execute("BEGIN IMMEDIATE")
        self.lost = self.recover()                            # told in the first reply
        self.socket.unlink(missing_ok=True)                   # a dead server's: we hold the lock
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.socket))
        self.listener.listen()
        self.listener.settimeout(TICK)

    def recover(self) -> dict | None:
        """A journal left behind: the last session ended without its COMMIT. Kept aside; → its changes net of undos
        ({"file", "changes": [{"prompt", "sql"}]}), or None. ponytail: told, never replayed."""
        if not self.journal.is_file() or not self.journal.stat().st_size:
            return None
        kept = self.dir / f"journal-lost-{datetime.now(UTC):%Y%m%dT%H%M%S%fZ}.jsonl"
        os.replace(self.journal, kept)
        changes = {}
        for line in kept.read_text().splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:                      # the line the crash cut
                continue
            if "undo" in entry:
                changes.pop(entry["undo"], None)
            else:
                changes[entry["n"]] = {"prompt": entry["prompt"], "sql": entry["sql"]}
        log(f"the last session wasn't saved: its journal is {kept.name}")
        return {"file": str(kept), "changes": list(changes.values())} if changes else None

    def write(self, entry: dict) -> None:
        with open(self.journal, "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def drop(self, n: int) -> None:
        self.db.execute(f"ROLLBACK TO c{n}")
        self.db.execute(f"RELEASE c{n}")

    def cancel(self, n: int, error: str) -> dict:
        self.drop(n)
        return {"ok": False, "error": f"{error}. Nothing was changed."}

    def message(self, text: str) -> dict:
        """A new message, pending until it runs to the end: one at a time, so it replaces one waiting for answers."""
        notices = [{"step": "message", "problem": f"The message waiting for answers ({self.pending['prompt']}) "
                                                  "was replaced by this one: its questions are dropped."}
                   ] if self.pending else []
        self.pending = {"prompt": text, "calls": {}, "open": [], "answered": [], "step": None, "shown": []}
        return self.resume(notices)

    def answer(self, question: str, given: str | None) -> dict:
        """The user's answer to an open question of the pending message, recorded as the model's certain answer;
        once none is open, the message runs again (its recorded calls replayed, not asked again)."""
        p = self.pending or {"open": []}
        try:
            q = questions.find(p, question, given, "", "No message is waiting for answers. Send it again: orbitrows "
                               "message … If it belongs to another store, add -s <store>.")
            match = questions.match(q, given)
        except questions.Invalid as e:
            return {"ok": False, "error": str(e)}
        confirm = q["question"].get("confirm")                # a change to every row, built: yes applies it, no drops it
        if confirm and match == "no":
            self.pending = None
            return {"ok": True, "ran": [], "notices": [{"step": "assembly", "request": q.get("request"),
                                                        "problem": "not applied: you answered no"}]}
        if save := q["question"].get("save"):                 # a unit conversion: kept, the message's rerun reads it
            from orbitrows.pipeline.merge import convert
            # Kept knowledge, not a change: no savepoint c<n> of its own, so nothing to undo. It is written inside
            # the open savepoints of the changes before it, which an undo rolls back: undo() puts it back after.
            convert.save(self.db, *save, match)
        questions.record(p, q, match, QUESTION, nulls=False)
        if confirm and not p["open"]:
            self.pending = None
            return self.apply(p["prompt"], p["built"].get("program") or [], [])
        if p["open"]:
            return asking(p, [])
        return self.resume([])

    def resume(self, notices: list) -> dict:
        """The pending message run by the handler: paused on its questions (kept, ids m_…), refused on its errors,
        else its program applied. Notices already told for this message aren't told again."""
        p = self.pending
        self.db.execute("SAVEPOINT handler")                  # whatever the handler leaves is never kept
        try:
            result = self.handle(self.db, p["prompt"], p["calls"])
        except BaseException:
            self.pending = None                               # ponytail: a failed run drops its answers; sent again
            raise
        finally:
            self.db.execute("ROLLBACK TO handler")
            self.db.execute("RELEASE handler")
        paused = result.get("paused")
        for n in (paused or result).get("notices") or []:
            if (key := json.dumps(n, sort_keys=True, ensure_ascii=False, default=str)) not in p["shown"]:
                p["shown"].append(key)
                notices.append(n)
        if paused:
            p["step"] = paused["step"]
            p["open"] = [q | {"id": "m_" + q["id"].removeprefix("q_")} for q in paused["questions"]]
            return asking(p, notices)
        if errors := result.get("errors"):
            self.pending = None
            return {"ok": False, "error": "\n  ".join(["The request couldn't be built, so nothing was changed:"] + [
                f"{e.get('request')} ({e.get('stage')}): {e.get('error')}" for e in errors])}
        if confirm := result.get("confirm"):                  # kept built: a yes applies it without running it again
            p["built"], p["step"] = result, "assembly"
            p["open"] = [{"id": f"m_all_{c['request']}", "request": c["request"], "step": "assembly",
                          "problem": f"This {c['problem']} ({c.get('rows')} rows), and the rows to change weren't "
                                     "found for sure. Apply it?", "question": {"type": "yes_no", "confirm": True}}
                         for c in confirm]
            return asking(p, notices)
        self.pending = None
        return self.apply(p["prompt"], result.get("program") or [], notices)

    def apply(self, text: str, program: list, notices: list) -> dict:
        """The program run in savepoint c<n>, its _history row and its journal line. A program that changes no row
        and no schema (SELECTs, an UPDATE matching nothing) is no change: rolled back, nothing to undo.
        → what ran, with the SELECTs' rows."""
        reply = {"ok": True, "ran": [], "notices": notices}
        if not program:
            return reply
        by = [(p["request"], s) for p in program for s in ([p["sql"]] if isinstance(p["sql"], str) else p["sql"])]
        statements = [s for _, s in by]
        if any(STRUCTURE.match(s) for s in statements):      # the export map and the classification would go stale
            return {"ok": False, "error": "Changing the table structure isn't supported yet: nothing was changed."}
        n, before, ddl = self.count + 1, self.db.total_changes, False
        self.db.execute(f"SAVEPOINT c{n}")
        try:
            return self.run(n, text, program, by, statements, before, ddl, reply)
        except BaseException:                                 # not a SQLite or journal error: c{n} must not stay applied
            self.drop(n)
            raise

    def run(self, n: int, text: str, program: list, by: list, statements: list, before: int, ddl: bool,
            reply: dict) -> dict:
        returned = {}                                         # request → the ids its RETURNING gave (assembly)
        for request, sql in by:
            try:
                cursor = self.db.execute(sql, {k: json.dumps(returned.get(k[5:], [])) for k in PASSED.findall(sql)})
                rows = cursor.fetchall() if RETURNING.search(sql) else cursor.fetchmany(ROWS + 1)
            except sqlite3.Error as e:
                return self.cancel(n, f"SQLite refused {sql}: {e}")
            if RETURNING.search(sql):
                returned.setdefault(request, []).extend(r[0] for r in rows)
            ran = {"request": request, "sql": sql}
            if RETURNING.search(sql):                         # a change a later request reads: its count
                reply["ran"].append(ran | {"changed": len(rows)})
            elif cursor.description:                          # a SELECT
                reply["ran"].append(ran | {"columns": [d[0] for d in cursor.description],
                                           "rows": rows[:ROWS]} | ({"more": True} if len(rows) > ROWS else {}))
            else:                                             # rowcount is -1 for DDL
                reply["ran"].append(ran | {"changed": max(cursor.rowcount, 0)})
            ddl = ddl or (not cursor.description and cursor.rowcount == -1)   # CREATE, ALTER… change no row
        if not ddl and self.db.total_changes == before:
            self.drop(n)
            return reply
        refresh(self.db, sorted({m[1] for s in statements if (m := WRITES.match(s))}))   # in c<n>: undone with it
        try:
            # once a request changes columns, their _store_columns.edited_by will be this row's id
            self.db.execute("INSERT INTO _history (created_at, name, prompt, json_ast, sql, classification_done) "
                            "VALUES (?, ?, ?, ?, ?, 1)",
                            (registry.now(), text if len(text) <= NAME else text[:NAME - 1] + "…", text,
                             json.dumps([p["ast"] for p in program if "ast" in p], ensure_ascii=False),
                             json.dumps(statements, ensure_ascii=False)))
            self.write({"n": n, "prompt": text, "sql": statements, "at": registry.now()})
        except (sqlite3.Error, OSError) as e:
            return self.cancel(n, f"The change couldn't be recorded: {e}")
        self.count = n
        self.changes.append((n, text))
        return reply | {"n": n}

    def incoming(self, folder: Path) -> dict:
        """A classified upload written in savepoint c<n> (the server's own DDL: not a message's, so not refused), its
        _history row and its journal line. → the written table."""
        from orbitrows.services.storage import incoming     # here: its imports a message doesn't need
        n = self.count + 1
        self.db.execute(f"SAVEPOINT c{n}")
        try:
            written = incoming.upload(self.db, self.dir, folder)
            refresh(self.db, [written["table"]])
            self.write({"n": n, "prompt": written["name"], "sql": written["sql"], "at": registry.now()})
        except (sqlite3.Error, OSError, ValueError, KeyError) as e:   # ValueError: a bad AST, bad JSON too
            return self.cancel(n, f"The upload couldn't be written: {type(e).__name__}: {e}")
        except BaseException:
            self.drop(n)
            raise
        self.count = n
        self.changes.append((n, written["name"]))
        self.uploads[n] = (folder, written["source_file"])
        return {"ok": True, "n": n} | {k: written[k] for k in ("id", "table", "source_file", "rows", "columns")}

    def undo(self) -> dict:
        if not self.changes:
            return {"ok": False, "error": "Nothing to undo: no change since the session started."}
        if self.pending:
            return {"ok": False, "error": "A message is waiting for answers: answer it or send another message first."}
        n, prompt = self.changes[-1]
        self.write({"undo": n, "at": registry.now()})         # first: a crash now loses the change anyway
        kept = self.db.execute("SELECT * FROM _conversions").fetchall() if self.db.execute(
            "SELECT 1 FROM sqlite_schema WHERE name = '_conversions'").fetchone() else []
        self.db.execute(f"ROLLBACK TO c{n}")
        self.db.execute(f"RELEASE c{n}")
        if kept:                                              # the formulas given since c<n> began: not the change's
            from orbitrows.pipeline.merge import convert
            convert.ensure(self.db)
            self.db.executemany("INSERT OR REPLACE INTO _conversions VALUES (?, ?, ?, ?, ?)", kept)
        self.changes.pop()
        if upload := self.uploads.pop(n, None):              # its CSV, never placed, goes with it
            shutil.rmtree(upload[0], ignore_errors=True)
        return {"ok": True, "undone": n, "prompt": prompt}

    def end(self) -> str | None:
        """COMMIT, then no more clients, and quick_check; the journal removed once saved. A COMMIT that fails (SQLITE_BUSY)
        leaves the transaction open and the server serving. → the error, or None."""
        try:
            self.db.execute("COMMIT")
        except (sqlite3.Error, OSError) as e:
            return f"{type(e).__name__}: {e}"
        for folder, source_file in self.uploads.values():     # saved: each upload's CSV moved under incoming/
            from orbitrows.services.storage import incoming
            try:
                incoming.place(self.dir, folder, source_file)
            except OSError as e:                              # only its CSV copy is missing from incoming/
                log(f"{source_file} not placed, its CSV is still in {folder}: {e}")
        self.uploads = {}
        self.close()
        try:
            if (ok := self.db.execute("PRAGMA quick_check").fetchone()[0]) != "ok":
                self.failed = f"session.db quick_check: {ok}"
            else:
                self.journal.unlink(missing_ok=True)
        except (sqlite3.Error, OSError) as e:
            self.failed = f"{type(e).__name__}: {e}"
        return self.failed

    def close(self) -> None:
        self.listener.close()
        self.socket.unlink(missing_ok=True)

    def serve(self, client: socket.socket) -> str | None:
        """One request, one reply. → "exit" once the session ended on it."""
        client.settimeout(READ)
        ended = None
        try:
            request = json.loads(client.makefile("rb").readline())
            match request.get("op"):
                case "message":
                    reply = self.message(str(request["text"]))
                case "answer":
                    reply = self.answer(str(request["id"]), request.get("answer"))
                case "incoming":
                    reply = self.incoming(Path(request["folder"]))
                case "undo":
                    reply = self.undo()
                case "status":
                    reply = {"ok": True, "pid": os.getpid(), "changes": len(self.changes)}
                case "export":                                # on the open transaction: unsaved changes included
                    from orbitrows.services.storage.export import export
                    try:
                        reply = {"ok": True, "files": export(self.db, self.dir, Path(request["folder"]),
                                                             bool(request.get("force")))}
                    except (FileExistsError, ValueError) as e:     # a file there, or a value its encoding can't hold
                        reply = {"ok": False, "error": str(e)}
                case "exit":
                    dropped = self.pending and self.pending["prompt"]
                    error = self.end()
                    ended = "exit" if not error or self.failed else None   # a failed COMMIT: still serving
                    reply = {"ok": False, "error": f"The session couldn't be saved: {error}"
                             + ("" if ended else ". It is still open: try orbitrows exit again.")} if error else \
                        {"ok": True, "saved": True} | ({"dropped": dropped} if dropped else {})
                case op:
                    reply = {"ok": False, "error": f"Unknown request: {op}"}
        except Exception as e:                                # a bad request or a failing handler: the session goes on
            log("".join(traceback.format_exception(e)))
            reply = {"ok": False, "error": f"The request failed: {type(e).__name__}: {e}"}
        if self.lost:
            reply["lost"], self.lost = self.lost, None
        try:
            client.sendall((json.dumps(reply, ensure_ascii=False, default=str) + "\n").encode())
        except OSError as e:                                  # the client left; what was applied stays applied
            log(f"reply not sent: {e}")
        return ended


def main(argv: list[str] | None = None) -> int:
    cli = argparse.ArgumentParser(prog="python -m orbitrows.services.session")
    cli.add_argument("store_id")
    cli.add_argument("--owner", type=int, help="the session ends when this process (a terminal's session) is gone")
    args = cli.parse_args(argv)
    lock = open(folder(args.store_id) / "session.lock", "w")  # held until this process ends, a kill too
    for _ in range(WAIT * 10):
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            time.sleep(0.1)
    else:
        log("another server holds this store's session")
        return 1
    stop = []
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, lambda number, frame: stop.append(signal.Signals(number).name))
    session = Session(args.store_id, handler())               # a bad ORBITROWS_HANDLER fails before the journal moves
    if session.handle is prompt:                              # the real pipeline (a test's handler loads nothing)
        if why := duckling.start():                           # the date reader's service
            log(f"dates can't be read this session: {why}")
        ready.preload()                                       # its models, while the first message is written
    idle = float(os.environ.get("ORBITROWS_IDLE") or IDLE)
    log(f"started, pid {os.getpid()}, owner {args.owner}, idle {idle} s")
    last, reason = time.monotonic(), None
    while not reason:
        try:
            client, _ = session.listener.accept()             # a signal is seen within TICK
        except TimeoutError:
            client = None
        if client:
            with client:
                reason = session.serve(client)
            last = time.monotonic()
        reason = reason or (stop and stop[0]) or (args.owner and not alive(args.owner) and "owner gone") \
            or (time.monotonic() - last > idle and "idle")
    error = session.failed if reason == "exit" else session.end()   # exit ended it already
    if error:
        session.close()
        log(f"ended ({reason}), NOT saved: {error}; the journal is kept")
        return 1
    log(f"ended ({reason}), saved" + (f"; the message waiting for answers ({session.pending['prompt']}) was dropped"
                                       if session.pending else ""))
    return 0
