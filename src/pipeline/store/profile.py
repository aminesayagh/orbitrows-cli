"""Import step after reading: facts about every column's values, counted by code, read by Jev as evidence.
Cells are compared as exact strings (outer spaces removed); nothing is interpreted or changed.

    profile(table)        {"rows", "profiled"?, "columns": {position: {"empty", "distinct", "max_repeat", "values"?,
                          "determines"?: [positions], "follows_row_order"?: true}}}
    named(column, header) one column's profile as Jev reads it: "determines" by header
    overlap(a, b)         share of a's distinct values found in b (a candidate reference between two tables)

A column is identified by its position in the file (0 for the first) in every import step: two columns may share a
header. "determines": the columns whose value is always the same for a given value of this column (customer_id →
customer_name, customer_email). Only asked for columns with repeated values; stops at the first counter-example.
"follows_row_order": every non-empty value is greater than the one before it in the file (at least two values),
counted over the whole file: numbers compared as numbers, text by its last run of digits when the text around it is
the same (A-1001, A-1002…). Evidence of an identifier assigned in order; a quantity jumps around (35, 7, 140).
Large tables are profiled on MAX_ROWS rows spread over the file ("profiled" says how many).
"""
import json
import re
from collections import Counter

FEW = 10                                                      # up to this many distinct values, they are listed
MAX_ROWS = 10_000  # ponytail: evenly spread sample; a streaming reader + SQLite COUNT(DISTINCT) if files outgrow it


def _rows(table):
    rows = table["rows"]
    if not rows:
        return []
    step = -(-len(rows) // MAX_ROWS)                          # ceil: 200k rows → every 20th row
    return rows[::step]


def values(table, i, rows=None):
    """The cells of the column at position i."""
    return [(r[i] if i < len(r) else "").strip() for r in (table["rows"] if rows is None else rows)]


def determines(a: list, b: list) -> bool:
    """For every value of a, b is always the same (empty a skipped). Stops at the first counter-example."""
    seen = {}
    for x, y in zip(a, b):
        if x and seen.setdefault(x, y) != y:
            return False
    return True


def _order_key(x: str):
    """(text before, number, text after) of a value: the number itself, or a text's last run of digits; else None."""
    try:
        return "", float(x), ""
    except ValueError:
        m = re.fullmatch(r"(.*?)(\d+)(\D*)", x)
        return (m[1], int(m[2]), m[3]) if m else None


def follows_row_order(v: list) -> bool:
    """Each non-empty value is greater than the one before it, with the same text around its number."""
    keys = [_order_key(x) for x in v if x]
    return len(keys) > 1 and all(a and b and a[0::2] == b[0::2] and a[1] < b[1] for a, b in zip(keys, keys[1:]))


def profile(table: dict) -> dict:
    rows = _rows(table)
    cells = {i: values(table, i, rows) for i in range(len(table["header"]))}
    columns = {}
    for i, v in cells.items():
        counts = Counter(x for x in v if x)
        columns[i] = {"empty": len(v) - sum(counts.values()), "distinct": len(counts),
                      "max_repeat": max(counts.values(), default=0)}
        if 0 < len(counts) <= FEW:
            columns[i]["values"] = sorted(counts)
        if follows_row_order(values(table, i)):                # the whole file: the order is the fact
            columns[i]["follows_row_order"] = True
    for i, c in columns.items():
        if c["max_repeat"] > 1 and c["distinct"] > 1:         # a unique or constant column tells nothing
            found = [g for g, d in columns.items() if g != i and d["distinct"] > 1 and determines(cells[i], cells[g])]
            if found:
                c["determines"] = found
    out = {"rows": len(table["rows"]), "columns": columns}
    return out | ({"profiled": len(rows)} if len(rows) < len(table["rows"]) else {})


def named(column: dict, header: list) -> dict:
    """One column's profile with "determines" by header, as Jev reads it."""
    return column | ({"determines": [header[i] for i in column["determines"]]} if "determines" in column else {})


def overlap(a: list, b: list) -> float:
    a, b = {x for x in a if x}, {x for x in b if x}
    return round(len(a & b) / len(a), 2) if a else 0.0


# ---- the stored profile: what the prompt pipeline knows of a table's values --------------------------------------
# After a message is sent, the prompt pipeline never reads a table's rows: the generated SQL is the only thing that
# does. What it knows of the values (a status column's statuses, a date column, an upload's number cells) is this
# profile, counted with SQL at import or upload and again after each change the session runs (refresh), kept in
# _profile next to the tables (a savepoint's undo restores it with them).

LISTED = 50                                                   # up to this many distinct values, they are kept
SAMPLES = 5
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
NUMBER = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)")    # an upload's number cell: a . for decimals, no unit
WHOLE = re.compile(r"[+-]?[0-9]+(\.0*)?")


def refresh(db, tables=None) -> None:
    """Count the profile of every column of `tables` (all the store's and uploads' tables when None) and keep it."""
    db.execute("CREATE TABLE IF NOT EXISTS _profile (table_name TEXT NOT NULL, column_name TEXT NOT NULL, "
               "profile_json TEXT NOT NULL, PRIMARY KEY (table_name, column_name)) STRICT")
    uploads = {n for (n,) in db.execute("SELECT sql_name FROM _incoming_tables")} if db.execute(
        "SELECT 1 FROM sqlite_schema WHERE name = '_incoming_tables'").fetchone() else set()
    if tables is None:
        tables = [n for (n,) in db.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE "
                                           "'\\_%' ESCAPE '\\' AND name NOT LIKE 'sqlite\\_%' ESCAPE '\\'")]
    for t in tables:
        db.execute("DELETE FROM _profile WHERE table_name = ?", (t,))
        for _, c, typ, *_ in db.execute(f'PRAGMA table_info("{t}")').fetchall():
            db.execute("INSERT INTO _profile VALUES (?, ?, ?)", (t, c, json.dumps(counted(db, t, c, t in uploads),
                                                                                  ensure_ascii=False)))


def counted(db, t: str, c: str, upload: bool) -> dict:
    filled = f'TRIM(COALESCE("{c}", \'\')) <> \'\''
    rows, distinct = db.execute(f'SELECT COUNT(*), COUNT(DISTINCT CASE WHEN {filled} THEN "{c}" END) FROM "{t}"').fetchone()
    empty, = db.execute(f'SELECT COUNT(*) FROM "{t}" WHERE NOT {filled}').fetchone()
    top = db.execute(f'SELECT "{c}", COUNT(*) AS n FROM "{t}" WHERE {filled} GROUP BY "{c}" ORDER BY n DESC LIMIT 3'
                     ).fetchall()
    out = {"rows": rows, "empty": empty, "distinct": distinct, "max_repeat": top[0][1] if top else 0}
    if repeats := [v for v, n in top if n > 1]:
        out["repeats"] = repeats
    if 0 < distinct <= LISTED:
        out["values"] = sorted((v for (v,) in db.execute(f'SELECT DISTINCT "{c}" FROM "{t}" WHERE {filled}')), key=str)
        if all(isinstance(v, str) and DATE.fullmatch(v) for v in out["values"]):
            out["dates"] = True
    elif distinct and not db.execute(f'SELECT 1 FROM "{t}" WHERE {filled} AND NOT ("{c}" GLOB '
                                     "'[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]') LIMIT 1").fetchone():
        out["dates"] = True
    if upload:                                                # every cell is TEXT: what its cells hold
        cells = [str(v).strip() for (v,) in db.execute(f'SELECT DISTINCT TRIM("{c}") FROM "{t}" WHERE {filled}')]
        out["cells"] = "TEXT" if not cells or not all(NUMBER.fullmatch(v) for v in cells) else \
            "INTEGER" if all(WHOLE.fullmatch(v) for v in cells) else "REAL"
        out["not_whole"] = [v for v in cells if not WHOLE.fullmatch(v)][:3]
        out["not_number"] = [v for v in cells if not NUMBER.fullmatch(v)][:3]
        out["samples"] = [v for (v,) in db.execute(f'SELECT "{c}" FROM "{t}" WHERE {filled} LIMIT {SAMPLES}')]
    return out


def facts(db, table: str, column: str) -> dict:
    """A column's stored profile; {} when it has none (a store never profiled: nothing is known of its values)."""
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE name = '_profile'").fetchone():
        return {}
    row = db.execute("SELECT profile_json FROM _profile WHERE table_name = ? AND column_name = ?",
                     (table, column)).fetchone()
    return json.loads(row[0]) if row else {}


def listed(db, table: str, column: str) -> list:
    """The column's values when it has few (LISTED at most: statuses, colours, brands), else []: a long text or a
    column of many values is never searched for a value."""
    return facts(db, table, column).get("values", [])
