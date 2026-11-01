"""The CLI's side of a store's session: one request to the store's server, which is started when none runs."""

import json
import os
import socket
import subprocess
import sys
import time

from orbitrows.services.storage import store as storage

START = 30                                                    # seconds a new server has to answer
SUN_PATH = 107                                                # bytes in a Unix socket's path (Linux), its NUL aside


class Unavailable(Exception):
    """The store's server can't be reached or started; the text says why."""


def send(path, request: dict) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.connect(str(path))
        s.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
        line = s.makefile("rb").readline()
    if not line:
        raise Unavailable(f"The store's session stopped without answering. Its log: {path.with_name('session.log')}")
    return json.loads(line)


def start(path) -> dict:
    """A detached server for the store, once it answers; → what its first reply told of the last session: the changes
    restored and those lost ({"restored"?, "lost"?}). Owned by this
    terminal's session only from a real terminal: a command run without one (an LLM's shell) owns nothing."""
    command = [sys.executable, "-m", "orbitrows.services.session", path.parent.name]
    if sys.stdin and sys.stdin.isatty():
        command += ["--owner", str(os.getsid(0))]
    with open(path.with_name("session.log"), "a") as log:
        server = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                  start_new_session=True)     # its own session: the terminal's SIGHUP isn't its
    deadline = time.monotonic() + START
    while time.monotonic() < deadline:
        try:
            first = send(path, {"op": "status"})
            return {k: first[k] for k in ("restored", "lost") if first.get(k)}
        except (FileNotFoundError, ConnectionRefusedError):   # not listening yet
            pass
        if server.poll() is not None:
            raise Unavailable(f"The store's session didn't start. Its log: {path.with_name('session.log')}")
        time.sleep(0.05)
    raise Unavailable(f"The store's session didn't answer within {START} s. Its log: {path.with_name('session.log')}")


def ask(store_id: str, request: dict, launch: bool = True) -> dict | None:
    """The server's reply: {"ok", "error"?, …, "lost"?: what the last session lost}. With no server running, one is
    started, or, if not launch, None."""
    path = storage.folder(store_id) / "session.sock"
    if len(os.fsencode(path)) > SUN_PATH:                     # ponytail: the socket stays in the store's folder
        raise Unavailable(f"The store's folder path is too long for its session socket ({path}). "
                          "Set XDG_DATA_HOME to a shorter folder.")
    try:
        return send(path, request)
    except FileNotFoundError:
        pass
    except ConnectionRefusedError:                            # a dead server's socket
        path.unlink(missing_ok=True)
    if not launch:
        return None
    told = start(path)
    try:
        reply = send(path, request)
    except OSError as e:
        raise Unavailable(f"The store's session stopped: {e}. Its log: {path.with_name('session.log')}") from None
    return reply | told
