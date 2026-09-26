"""Turn unexpected errors into a short message; full details go to a log file, never the terminal."""

import os
import traceback
from datetime import datetime
from pathlib import Path

from rich.text import Text


def log_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "orbitrows"


def report(error: BaseException) -> Text:
    """Write the traceback (no local variables, so no secrets) to a log file; return the user-facing message."""
    try:
        folder = log_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"crash-{datetime.now():%Y%m%d-%H%M%S}.log"
        path.write_text("".join(traceback.format_exception(error)))
        where = f"Details were saved to {path}"
    except OSError:
        where = "The details could not be saved."
    return Text.assemble(
        ("OrbitRows hit an unexpected error and had to stop.\n", "bold red"),
        f"{type(error).__name__}. {where}\n",
        ("Please include that file when you report the problem.", "dim"),
    )
