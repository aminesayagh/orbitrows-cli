"""Phase 1: the technical inventory. Every SQLGlot function, operator and predicate class, with its names and its AST
arguments, tried on SQLite: written in SQLite's dialect, then prepared by SQLite (EXPLAIN: no row read). What passes
is what the engine runs; the rest stays listed, refused. Not a product catalog: no descriptions, outcomes or examples
(SQLGlot has none), and arg_types are AST fields, not a SQL signature.

    uv run python -m orbitrows.pipeline.operations.inventory     the counts, by kind, and the supported names
"""
import inspect
import sqlite3
from itertools import product

from sqlglot import exp

COLUMNS = {"number": exp.column("n"), "text": exp.column("s"), "date": exp.column("d")}
KINDS = ("aggregate", "window", "function", "operator", "predicate")
SKIP = (exp.Func, exp.AggFunc, exp.Binary, exp.Predicate, exp.Unary, exp.Connector)   # bases, not operations


def kind(cls) -> str | None:
    if issubclass(cls, exp.AggFunc):
        return "aggregate"
    if issubclass(cls, exp.Func):
        return "function"
    if issubclass(cls, exp.Predicate):
        return "predicate"
    if issubclass(cls, (exp.Binary, exp.Unary)):
        return "operator"
    return None


def classes() -> list:
    return sorted((c for c in vars(exp).values() if inspect.isclass(c) and issubclass(c, exp.Expression)
                   and c not in SKIP and kind(c)), key=lambda c: c.__name__)


def tried(db, cls) -> dict | None:
    """The first arguments SQLite accepts, as plain SQL and, for a function, over a window (OVER ()); None when none
    does. Each argument is a number, a text or a date column (the first three tried in every combination); the
    required ones first, then the optional ones too (SQLGlot's AST may call optional what SQLite needs: COALESCE's
    second value, SUBSTR's start); a list field ("expressions") gets one column. Flags (is_*, zero_start…) are left out."""
    required = [k for k, v in cls.arg_types.items() if v]
    optional = [k for k, v in cls.arg_types.items() if not v and not k.startswith(("is_", "zero_")) and k != "safe"]
    for fields in (required, required + optional):
        for types in product(COLUMNS, repeat=min(len(fields), 3)):
            given = dict(zip(fields, types)) | {k: "number" for k in fields[3:]}
            args = {k: [COLUMNS[t].copy()] if k == "expressions" else COLUMNS[t].copy() for k, t in given.items()}
            try:
                node = cls(**args)
            except Exception:
                continue
            for over in ((False, True) if issubclass(cls, exp.Func) else (False,)):
                try:
                    sql = (exp.Window(this=node.copy()) if over else node).sql(dialect="sqlite",
                                                                               unsupported_level="raise")
                    db.execute(f"EXPLAIN SELECT {sql} FROM t").fetchall()
                except Exception:
                    continue
                return {"sql": sql, "arguments": given, "window": over}
    return None


def inventory() -> list[dict]:
    """[{"class", "kind", "names", "arguments": {name: required}, "sqlite": {"sql", "arguments", "window"} | None}]"""
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE t (n REAL, s TEXT, d TEXT)")
    out = []
    for cls in classes():
        try:
            names = list(cls.sql_names()) if issubclass(cls, exp.Func) else [cls.key.upper()]
        except Exception:
            names = [cls.key.upper()]
        found = tried(db, cls)
        k = kind(cls)
        if found and found["window"]:
            k = "window"                                      # runs only over a window: ROW_NUMBER, RANK, LAG…
        out.append({"class": cls.__name__, "kind": k, "names": names,
                    "arguments": {a: bool(r) for a, r in cls.arg_types.items()}, "sqlite": found})
    return out


if __name__ == "__main__":
    rows = inventory()
    for k in KINDS:
        mine = [r for r in rows if r["kind"] == k]
        ok = [r for r in mine if r["sqlite"]]
        print(f"{k:10} {len(ok):4} of {len(mine):4} run on SQLite")
    print("supported:", ", ".join(sorted(r["names"][0] for r in rows if r["sqlite"])))
