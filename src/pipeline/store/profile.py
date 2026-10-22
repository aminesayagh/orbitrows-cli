"""Import step after reading: facts about every column's values, counted by code, read by Jev as evidence.
Cells are compared as exact strings (outer spaces removed); nothing is interpreted or changed.

    profile(table)        {"rows", "profiled"?, "columns": {position: {"empty", "distinct", "max_repeat", "values"?,
                          "determines"?: [positions]}}}
    named(column, header) one column's profile as Jev reads it: "determines" by header
    overlap(a, b)         share of a's distinct values found in b (a candidate reference between two tables)

A column is identified by its position in the file (0 for the first) in every import step: two columns may share a
header. "determines": the columns whose value is always the same for a given value of this column (customer_id →
customer_name, customer_email). Only asked for columns with repeated values; stops at the first counter-example.
Large tables are profiled on MAX_ROWS rows spread over the file ("profiled" says how many).
"""
from collections import Counter

FEW = 10                                                      # up to this many distinct values, they are listed
MAX_ROWS = 10_000  # ponytail: evenly spread sample; a streaming reader + SQLite COUNT(DISTINCT) if files outgrow it


def _rows(table):
    rows = table["rows"]
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
