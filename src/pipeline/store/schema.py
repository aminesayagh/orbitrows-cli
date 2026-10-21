"""Import step 7: the store schema (diagram "the algorithm concludes a schema of the table"). No Jev call: the
decisions of steps 1–6 are assembled into the final AST, validated, and mapped back to the uploaded files.
Store files only; the output is the AST (no SQL text kept, no rows loaded).

    code  a joined group (step 6) → its own table: the group key is its primary key, its columns move there, and the
          table that held the key references it (a group inside a group: orders → customers); named from the key
          (customer_id → customer), else <file>_<key>
    code  a table without a primary key → an added `id INTEGER` key (`row_id` when `id` is taken; SQLite numbers it)
    code  metadata: one row per column (table `_columns`): source file, header and position, role, kind, level,
          context; the export map: how to rebuild each uploaded file, column by column, along the references
    code  validation: Pydantic + SQLite STRICT per table, check_references across the store
    proof round_trip(): load the rows into an in-memory SQLite along the schema, rebuild every file, compare cell by cell
"""
import json
import re
import sqlite3

from .types import EXPORTED
from orbitrows.sql.ddl import CreateTable, ForeignKey, NewColumn, NewTable, TableColumns, check_references, ddl, sql_name

KEY_SUFFIX = re.compile(r"_(id|sku|ref|code|no|nr|num|number)$")
META_COLUMNS = [("table_id", "TEXT", False), ("column_id", "TEXT", False), ("source_file", "TEXT", True),
                ("source_header", "TEXT", True), ("position", "INTEGER", True), ("role", "TEXT", True),
                ("kind", "TEXT", True), ("level", "TEXT", True), ("unit", "TEXT", True), ("unit_kind", "TEXT", True),
                ("unit_from", "TEXT", True), ("context_table_id", "TEXT", True), ("context_column_id", "TEXT", True),
                ("edited_by", "TEXT", False), ("extra_json", "TEXT", True)]
# ponytail: the metadata's natural key (table, column) is composite; it is the import's own table, not a merchant's
METADATA = NewTable(table_id="t__columns", sql_name="_columns", primary_key=["c_table_id", "c_column_id"],
                    columns=[NewColumn(column_id=f"c_{n}", sql_name=n, header=None, sql_type=t, nullable=null)
                             for n, t, null in META_COLUMNS])


def _name(key: str, file: str, taken: set) -> str:
    base = KEY_SUFFIX.sub("", key)
    name = base if base and base not in taken else sql_name(f"{file}_{base or key}", taken)
    taken.add(name)
    return name


def _split(file: str, s: dict, taken: set) -> list[dict]:
    """The file's table and its group tables: [{"t": NewTable fields, "parent": (table_id, fk column) | None}],
    the file's own table first."""
    d = NewTable(**s["schema"]["create_table"]["definition"])
    by_h = {c.header: c for c in d.columns}                   # ponytail: a repeated header maps to its first column
    groups = (s.get("joins") or {}).get("groups", [])
    moved = {h for g in groups for h in g["columns"]}
    specs = [{"t": {"table_id": d.table_id, "sql_name": d.sql_name, "primary_key": d.primary_key, "foreign_keys": [],
                    "columns": [c for c in d.columns if c.header not in moved]}, "parent": None}]
    for g in groups:
        key = by_h[g["key"]]
        name = _name(key.sql_name, d.sql_name, taken)
        specs.append({"t": {"table_id": f"t_{name}", "sql_name": name, "primary_key": [key.column_id],
                            "foreign_keys": [], "columns": [key.model_copy(update={"nullable": False})]
                            + [by_h[h] for h in g["columns"]]}, "parent": None})
    home = {c.column_id: sp for sp in specs for c in sp["t"]["columns"] if c.column_id not in sp["t"]["primary_key"]}
    home |= {c.column_id: specs[0] for c in specs[0]["t"]["columns"]}
    for fk in d.foreign_keys:                                 # step 4's references follow their column
        home[fk.columns[0]]["t"]["foreign_keys"].append(fk)
    for sp in specs[1:]:                                      # the table holding a group key references the group
        k = sp["t"]["primary_key"][0]
        holder = home[k]["t"]
        holder["foreign_keys"].append(ForeignKey(columns=[k], references=TableColumns(table=sp["t"]["table_id"], columns=[k])))
        sp["parent"] = (holder["table_id"], k)
    for sp in specs:                                          # no key: an added id
        t = sp["t"]
        if not t["primary_key"]:
            names = {c.sql_name for c in t["columns"]}
            n = "id" if "id" not in names else sql_name("row_id", names)
            t["columns"] = [NewColumn(column_id=f"c_{n}", sql_name=n, header=None, sql_type="INTEGER", nullable=False)] + t["columns"]
            t["primary_key"] = [f"c_{n}"]
    return specs


def assemble(files: dict) -> dict:
    """files: {"<file>.csv": its saved sections (read, schema, roles, context, joins)}, the store files of one session →
    {"tables": [create_table], "metadata_table": create_table, "metadata": [rows], "export": [per file], "problems": [...]}"""
    taken = {s["schema"]["create_table"]["definition"]["sql_name"] for s in files.values()}
    tables, export, metadata = [], [], []
    for file, s in files.items():
        specs = _split(file, s, taken)
        ids = {sp["t"]["table_id"]: sp for sp in specs}

        def via(sp):
            return [] if not sp["parent"] else via(ids[sp["parent"][0]]) + [sp["parent"][1]]
        where = {}                                            # header → (table_id, column_id, via)
        for sp in specs:
            for c in sp["t"]["columns"]:
                if c.header is not None and (c.column_id not in sp["t"]["primary_key"] or sp is specs[0]):
                    where.setdefault(c.header, (sp["t"]["table_id"], c.column_id, via(sp)))
        header = s["read"]["header"]
        export.append({"file": file, "has_header": s["read"]["has_header"], "root": specs[0]["t"]["table_id"],
                       "columns": [{"header": h, "table_id": where[h][0], "column_id": where[h][1], "via": where[h][2]}
                                   for h in header]})
        roles, ctx = s.get("roles", {}).get("columns", {}), s.get("context", {})
        cc, sc = ctx.get("context_columns", {}), ctx.get("source_context", {})
        for sp in specs:
            t = NewTable(**sp["t"])
            tables.append(CreateTable(id=f"r_create_{t.sql_name}", definition=t))
            for c in t.columns:
                r = roles.get(c.header, {})
                row = {"table_id": t.table_id, "column_id": c.column_id, "source_file": file if c.header is not None else None,
                       "source_header": c.header, "position": header.index(c.header) if c.header in header else None,
                       "role": r.get("role"), "kind": r.get("kind"), "level": r.get("level"), "unit": None,
                       "unit_kind": None, "unit_from": None, "context_table_id": None, "context_column_id": None,
                       "edited_by": "import", "extra_json": None}
                if c.header in cc:                            # a context column: kept as a column, its links here
                    e = cc[c.header]
                    row |= {"unit": e.get("unit"), "unit_kind": e["kind"], "extra_json": json.dumps(
                        {"context": e["context"], "describes": [list(where[h][:2]) for h in e["describes"]]}
                        | ({"flagged": True} if e.get("flagged") else {}), ensure_ascii=False)}
                elif src := sc.get(c.header):
                    row |= {"unit": src.get("unit"), "unit_kind": src["kind"], "unit_from": src["from"]}
                    if src["from"] == "column":
                        row |= dict(zip(("context_table_id", "context_column_id"), where[src["column"]][:2]))
                if r.get("flagged") or sc.get(c.header, {}).get("flagged"):
                    row["extra_json"] = json.dumps(json.loads(row["extra_json"] or "{}") | {"flagged": True})
                metadata.append(row)
    problems = check_references([t.definition for t in tables])
    return {"tables": [t.model_dump() for t in tables], "metadata_table": CreateTable(
        id="r_create__columns", definition=METADATA).model_dump(), "metadata": metadata, "export": export,
        "problems": [{"table_id": t, "problem": p} for t, p in problems]}


def _text(v) -> str:
    return "" if v is None else str(v)


def _same_number(a: str, b: str) -> bool:
    """a is written as a database exports a number and holds the same value (12.50 → 12.5); "007" → 7 is not."""
    try:
        return EXPORTED["REAL"].fullmatch(a) is not None and float(a) == float(b)
    except ValueError:
        return False


def round_trip(store: dict, rows: dict) -> list[dict]:
    """rows: {"<file>.csv": its data rows}. Loads them into an in-memory SQLite along the store schema (group rows
    merged by key), rebuilds every file from the export map, and lists what differs; [] proves nothing is lost.
    A cell rebuilt as the same number written otherwise (a REAL column: 12.50 → 12.5) is kind "number_format";
    any other difference is kind "value"."""
    db = sqlite3.connect(":memory:")
    defs = {t["definition"]["table_id"]: NewTable(**t["definition"]) for t in store["tables"]}
    for d in defs.values():
        db.execute(ddl(d, defs))
    q = lambda n: f'"{n}"'
    diffs = []
    for ex in store["export"]:
        pos = {c["header"]: i for i, c in reversed(list(enumerate(ex["columns"])))}
        by_table = {t_id: [(c.column_id, pos[c.header]) for c in defs[t_id].columns if c.header is not None]
                    for t_id in dict.fromkeys(c["table_id"] for c in ex["columns"])}
        for n, row in enumerate(rows[ex["file"]]):
            cell = lambda i: (row[i] if i < len(row) else "") or None
            for t_id, cols in sorted(by_table.items(), key=lambda x: x[0] != ex["root"]):
                d = defs[t_id]
                names = {c.column_id: c.sql_name for c in d.columns}
                vals = {cid: cell(i) for cid, i in cols}
                if t_id == ex["root"] and d.primary_key[0] not in vals:   # the added id: the row number
                    vals[d.primary_key[0]] = n + 1
                elif t_id != ex["root"] and vals[d.primary_key[0]] is None:
                    continue                                  # no key on this row: no group record
                sql = (f'INSERT {"" if t_id == ex["root"] else "OR IGNORE "}INTO {q(d.sql_name)} '
                       f'({", ".join(q(names[c]) for c in vals)}) VALUES ({", ".join("?" * len(vals))})')
                try:
                    db.execute(sql, list(vals.values()))
                except sqlite3.Error as e:
                    diffs.append({"file": ex["file"], "row": n, "problem": f"{d.sql_name}: {e}", "kind": "value"})
        # rebuild: the root, then one LEFT JOIN per reference path
        root = defs[ex["root"]]
        alias, joins = {(): "r0"}, []
        for c in ex["columns"]:
            path = tuple(c["via"])
            for i in range(1, len(path) + 1):
                if path[:i] in alias:
                    continue
                parent = defs[_table_at(defs, ex["root"], path[:i - 1])]
                target = defs[_table_at(defs, ex["root"], path[:i])]
                a = alias[path[:i]] = f"r{len(alias)}"
                col = {x.column_id: x.sql_name for x in parent.columns}[path[i - 1]]
                pk = {x.column_id: x.sql_name for x in target.columns}[target.primary_key[0]]
                joins.append(f'LEFT JOIN {q(target.sql_name)} {a} ON {a}.{q(pk)} = {alias[path[:i - 1]]}.{q(col)}')
        select = [f'{alias[tuple(c["via"])]}.{q({x.column_id: x.sql_name for x in defs[c["table_id"]].columns}[c["column_id"]])}'
                  for c in ex["columns"]]
        got = db.execute(f'SELECT {", ".join(select)} FROM {q(root.sql_name)} r0 {" ".join(joins)} ORDER BY r0.rowid').fetchall()
        want = rows[ex["file"]]
        if len(got) != len(want):
            diffs.append({"file": ex["file"], "problem": f"{len(got)} rows rebuilt, {len(want)} uploaded", "kind": "value"})
        for n, (g, w) in enumerate(zip(got, want)):
            for i, c in enumerate(ex["columns"]):
                up, back = (w[i] if i < len(w) else ""), _text(g[i])
                if up != back:
                    diffs.append({"file": ex["file"], "row": n, "header": c["header"], "uploaded": up, "rebuilt": back,
                                  "kind": "number_format" if _same_number(up, back) else "value"})
    for table, rowid, parent, _ in db.execute("PRAGMA foreign_key_check"):     # SQLite checks every reference
        diffs.append({"table": table, "rowid": rowid, "problem": f"references a missing {parent} record", "kind": "value"})
    return diffs


def _table_at(defs, root, path) -> str:
    """The table reached from the root by following the reference columns in path."""
    t = root
    for k in path:
        t = next(fk.references.table for fk in defs[t].foreign_keys if fk.columns == [k])
    return t

