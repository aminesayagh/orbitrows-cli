"""A store's files written back from its tables (`orbitrows export`): each uploaded file rebuilt along the export map
kept in session.db (`_export`, its ids named by `_store_tables`/`_store_columns`, its joins SQLite's foreign keys), by
the rebuild the import's round-trip proof runs. Each file is written like its upload in files/: same name, encoding,
delimiter and line ending, the header row only if it had one; NULL is an empty cell, a number is written as stored
(12.50 comes back 12.5)."""
import csv
import json
import os
import tempfile
from pathlib import Path

from orbitrows.pipeline.store.schema import _text, rebuild
from orbitrows.services.csv.reader import read_table


class Unwritable(ValueError):
    """A value the file's encoding can't hold (a message wrote it): that file isn't written."""


def export(db, store: Path, out: Path, force: bool = False) -> list[dict]:
    """Every file of the store at `store` (its folder) from db (session.db, or the session's open transaction) into
    the folder out. Raises FileExistsError, before writing anything, when a file is already there and not force.
    → [{"file", "path", "rows"}], in upload order."""
    files = db.execute("SELECT file, has_header, root_table_id, spec_json FROM _export ORDER BY rowid").fetchall()
    tables = dict(db.execute("SELECT id, sql_name FROM _store_tables"))
    columns = dict(db.execute("SELECT id, sql_name FROM _store_columns"))
    if not force and (taken := [str(out / f) for f, *_ in files if (out / f).exists()]):
        raise FileExistsError(f"Already there: {', '.join(taken)}. Nothing was written; add --force to replace.")
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for file, has_header, root, spec in files:
        spec = json.loads(spec)
        rows = rebuild(db, tables[root], [([columns[k] for k in c["via"]], columns[c["column_id"]]) for c in spec])
        upload = store / "files" / file
        read = read_table(upload, bool(has_header))
        ending = "\r\n" if b"\r\n" in upload.read_bytes() else "\n"
        fd, tmp = tempfile.mkstemp(dir=out, prefix=f".{file}.", suffix=".tmp")
        try:
            with open(fd, "w", encoding=read["encoding"], newline="") as f:
                w = csv.writer(f, delimiter=read["delimiter"], lineterminator=ending)
                if has_header:
                    w.writerow(c["header"] for c in spec)
                w.writerows([_text(v) for v in row] for row in rows)
            os.replace(tmp, out / file)                       # never a half-written file under the final name
        except BaseException as e:
            Path(tmp).unlink(missing_ok=True)
            if not isinstance(e, UnicodeEncodeError):
                raise
            before = f" (written before it: {', '.join(x['file'] for x in written)})" if written else ""
            raise Unwritable(f"{file} is {read['encoding']}, which can't hold {e.object[e.start:e.end]!r}: it wasn't "
                             f"written{before}.") from None
        written.append({"file": file, "path": str(out / file), "rows": len(rows)})
    return written
