"""Unit conversions for a merge: never automatic. A linked incoming column whose unit differs from its store column's
asks the user one formula from x (x * 10.85), kept in _conversions by (store column, from unit, to unit) so a later
merge doesn't ask again. Units that vary row by row (a context column, or cells writing their own) are refused: the
user prepares that column differently. The formula becomes an expression of the merge's SQL AST (parse())."""

import ast
import hashlib
import math
import sqlite3
from pathlib import Path

from orbitrows.services.storage import registry
from orbitrows.sql.dml import Arithmetic, Column, Lit, Negate

X = Column(type="column", source="", reference="x")      # the incoming value: the caller puts its column here
EXAMPLE = "x * 10.85, or (x - 32) / 1.8"
OPERATORS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/"}
SCHEMA = """CREATE TABLE IF NOT EXISTS _conversions (
    store_column_id INTEGER NOT NULL REFERENCES _store_columns(id),
    from_unit       TEXT NOT NULL,                     -- '' when that side has no unit
    to_unit         TEXT NOT NULL,
    formula         TEXT NOT NULL,                     -- from x, as the user gave it (parse() checked it)
    created_at      TEXT NOT NULL,
    PRIMARY KEY (store_column_id, from_unit, to_unit)
) STRICT"""


def parse(text: str):
    """The formula as an AST expression, X standing for the value. Only x, finite numbers, + - * / and parentheses,
    with x at least once and no division by a literal 0: a whitelist over Python's parser, never eval. Else
    ValueError."""
    try:
        tree = ast.parse(text.strip(), mode="eval").body
    except SyntaxError:
        raise ValueError(f"{text!r} isn't a formula. Give one from x, e.g. {EXAMPLE}") from None
    out = _expression(tree, text)
    if not any(isinstance(n, ast.Name) and n.id == "x" for n in ast.walk(tree)):
        raise ValueError(f"{text!r} doesn't use x: every value would become the same. Give one from x, e.g. {EXAMPLE}")
    return out


def _expression(node: ast.AST, text: str):
    if isinstance(node, ast.Name) and node.id == "x":
        return X
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):   # not a bool, str or complex
        if not math.isfinite(node.value):                     # 1e999
            raise ValueError(f"{text!r}: {ast.unparse(node)} isn't a finite number. Give one from x, e.g. {EXAMPLE}")
        return Lit(type="literal", value=node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        operand = _expression(node.operand, text)
        return Negate(type="negate", operand=operand) if isinstance(node.op, ast.USub) else operand
    if isinstance(node, ast.BinOp) and type(node.op) in OPERATORS:
        divisor = node.right.operand if isinstance(node.right, ast.UnaryOp) else node.right
        if isinstance(node.op, ast.Div) and isinstance(divisor, ast.Constant) and divisor.value == 0:
            raise ValueError(f"{text!r}: a division by 0. Give one from x, e.g. {EXAMPLE}")
        return Arithmetic(type="arithmetic", operator=OPERATORS[type(node.op)],
                          left=_expression(node.left, text), right=_expression(node.right, text))
    what = f"name {node.id}" if isinstance(node, ast.Name) else ast.unparse(node)
    raise ValueError(f"{text!r}: {what} isn't allowed. Only x, numbers, + - * / and parentheses, e.g. {EXAMPLE}")


def ensure(db: sqlite3.Connection) -> None:
    """_conversions, on the caller's connection (the session owns the transaction)."""
    db.execute(SCHEMA)


def save(db: sqlite3.Connection, store_column_id: int, from_unit: str | None, to_unit: str | None,
         formula: str) -> None:
    """The user's formula for (store column, from, to): a new answer replaces the old one."""
    parse(formula)
    ensure(db)
    db.execute("INSERT OR REPLACE INTO _conversions VALUES (?, ?, ?, ?, ?)",
               (store_column_id, from_unit or "", to_unit or "", formula.strip(), registry.now()))


def lookup(db: sqlite3.Connection, store_column_id: int, from_unit: str | None, to_unit: str | None) -> str | None:
    """The formula kept for (store column, from, to), or None: none given yet (or no _conversions yet)."""
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name = '_conversions'").fetchone():
        return None
    row = db.execute("SELECT formula FROM _conversions WHERE store_column_id = ? AND from_unit = ? AND to_unit = ?",
                     (store_column_id, from_unit or "", to_unit or "")).fetchone()
    return row and row[0]


def _side(unit_id, unit, unit_from, context_column_id) -> str | None:
    """Why a column's unit varies row by row, or None: one unit for the whole column (or none)."""
    if context_column_id is not None or unit_from == "column":
        return "its unit comes from another column"
    if unit_id is not None and unit is None:                  # a context row without a unit: the cells name it
        return "its unit varies row by row"
    return None


def plan(db: sqlite3.Connection, upload_id: int) -> dict:
    """The linked column pairs of the incoming table (_incoming_tables.id) with a unit on either side → {"needs":
    [{"pair", "from", "to", "kind", "samples"}] (a formula to ask), "known": [{"pair", "formula"}] (asked before),
    "refused": [{"pair", "reason"}] (row-varying units), "same": [pairs]}."""
    table = db.execute("SELECT sql_name FROM _incoming_tables WHERE id = ?", (upload_id,)).fetchone()[0]
    rows = db.execute(
        "SELECT m.incoming_column_id, m.store_column_id, ic.sql_name, "
        "ic.unit_id, iu.unit, iu.unit_kind, iu.unit_from, ic.context_column_id, "
        "sc.unit_id, su.unit, su.unit_kind, su.unit_from, sc.context_column_id "
        "FROM _incoming_store_matches m JOIN _incoming_columns ic ON ic.id = m.incoming_column_id "
        "JOIN _store_columns sc ON sc.id = m.store_column_id "
        "LEFT JOIN _column_context iu ON iu.id = ic.unit_id LEFT JOIN _column_context su ON su.id = sc.unit_id "
        "WHERE ic.table_id = ? ORDER BY m.incoming_column_id, m.store_column_id", (upload_id,)).fetchall()
    found = {"needs": [], "known": [], "refused": [], "same": []}
    for i, s, name, i_id, i_unit, i_kind, i_from, i_ctx, s_id, s_unit, s_kind, s_from, s_ctx in rows:
        pair = (i, s)
        if i_id is None and s_id is None and i_ctx is None and s_ctx is None:
            continue                                          # no unit on either side: nothing to convert
        mine, theirs = _side(i_id, i_unit, i_from, i_ctx), _side(s_id, s_unit, s_from, s_ctx)
        if why := mine or theirs:
            side = "the incoming column" if mine else "the store column"
            found["refused"].append({"pair": pair, "reason": f"{side}: {why}. A merge needs one unit per column: "
                                     "split it by unit, or convert it with a script, then upload it again."})
        elif i_unit == s_unit:
            found["same"].append(pair)
        elif formula := lookup(db, s, i_unit, s_unit):
            found["known"].append({"pair": pair, "formula": formula})
        else:
            samples = [v for (v,) in db.execute(
                f'SELECT "{name}" FROM "{table}" WHERE TRIM(COALESCE("{name}", \'\')) != \'\' LIMIT 5')]
            found["needs"].append({"pair": pair, "from": i_unit, "to": s_unit, "kind": i_kind or s_kind,
                                   "samples": samples})
    return found


def question(db: sqlite3.Connection, need: dict) -> dict:
    """The formula question of a need, in the CLI's question shape; its id is stable for the pair."""
    i, s = need["pair"]
    header, file = db.execute("SELECT COALESCE(c.source_header, c.sql_name), t.source_file FROM _incoming_columns c "
                              "JOIN _incoming_tables t ON t.id = c.table_id WHERE c.id = ?", (i,)).fetchone()
    store_header, store_table = db.execute(
        "SELECT COALESCE(c.source_header, c.sql_name), t.sql_name FROM _store_columns c "
        "JOIN _store_tables t ON t.id = c.table_id WHERE c.id = ?", (s,)).fetchone()
    unit = lambda u: u or "no stated unit"                    # one side without a unit still needs a formula
    return {"id": "c_" + hashlib.sha1(f"{i}:{s}".encode()).hexdigest()[:10], "step": "conversion",
            "problem": f"{header} ({Path(file).name}) is in {unit(need['from'])}, the store's {store_table}."
                       f"{store_header} is in {unit(need['to'])}. Give the formula from x, e.g. x * 10.85 "
                       f"(samples: {', '.join(map(str, need['samples']))}).",
            "question": {"type": "formula"}}
