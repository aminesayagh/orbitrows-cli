"""Step 7 of prompt → AST: assembly and validation (plan S6, S8–S10, decided 2026-10-12). Code only, no Jev: every
decision was made by steps 1–6; this step builds one complete AST statement per request and checks it. It never runs
it: the session's execution does, and reports what it did (rows changed, rows read).

    aliases      one per table of a statement; the steps' column nodes (source = table id) are rewritten to them
    other tables a condition on another table follows the foreign keys from the main table (variants → products →
                 suppliers) as a chain of IN (SELECT …); a value shown from another table is a one-row subquery
                 along many-to-one keys (a product's name for a variant); one-to-many is warned, never guessed
    scope        the condition tree (step 6), all rows, or the rows of an earlier request ("those"):
                   after a select   id IN (SELECT id with that select's own conditions, order and limit)
                   after an insert, update or delete   the ids it changed: it runs with RETURNING and the later
                                    request reads id IN (SELECT value FROM json_each(:rows_r1)), the ids the session
                                    passes as it runs the program (notice #11)
    order        the requests' order (step 1's links); AND requests run as written
    validation   Pydantic (each statement's shape), the compiler against the schema as the statements change it (a
                 column added by r1 exists for r2), SQLite's prepare (EXPLAIN, no row read): an error stops the run
    questions    every warning of steps 1–7 in one list, with its step; this step adds: every row changed (the
                 statement has no condition), a statement SQLite refused

Section "assembly": {"program": [{"request", "ast", "sql", "rows_from"?, "returning"?}], "questions": [...],
"errors": [...]}.
"""
import json
from collections import deque

from orbitrows.sql.sql import check, program
from .parts import main_table as parts_main_table

ROWS = ("select", "update", "delete")


# ---- schema helpers -------------------------------------------------------------------------------------------

def defs(schema):
    return {t["definition"]["table_id"]: t["definition"] for t in schema}


def pk(schema, table):
    d = defs(schema).get(table)
    return d["primary_key"][0] if d and d.get("primary_key") else None


def edges(schema):
    """Foreign keys both ways: (from table, from column, to table, to column, "up" = child → parent | "down")."""
    out = []
    for t, d in defs(schema).items():
        for fk in d.get("foreign_keys", []):
            (c,), ref = fk["columns"], fk["references"]
            out.append((t, c, ref["table"], ref["columns"][0], "up"))
            out.append((ref["table"], ref["columns"][0], t, c, "down"))
    ups = {e[:4] for e in out if e[4] == "up"}                # a key both ways (an upload's, one-to-one): up each way
    return [e for e in out if e[4] == "up" or e[:4] not in ups]


def path(schema, src, dst):
    """The shortest chain of foreign keys from src to dst, or None."""
    if src == dst:
        return []
    seen, todo = {src}, deque([(src, [])])
    while todo:
        t, p = todo.popleft()
        for e in edges(schema):
            if e[0] == t and e[2] not in seen:
                if e[2] == dst:
                    return p + [e]
                seen.add(e[2])
                todo.append((e[2], p + [e]))
    return None


# ---- AST helpers ----------------------------------------------------------------------------------------------

def col(alias, cid):
    return {"type": "column", "source": alias, "reference": cid}


def rewrite(node, mapping: dict):
    """Column sources: table id → alias."""
    s = json.dumps(node)
    for t, a in mapping.items():
        s = s.replace(f'"source": "{t}"', f'"source": "{a}"')
    return json.loads(s)


def tables_in(node) -> set:
    """The tables a node reads; a subquery's own alias is not one (step 6's comparison with a linked table's column,
    correlated through the keys, stays a condition on its own table)."""
    out, own = set(), set()

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "database_table" and "alias" in n:
                own.add(n["alias"])
            if n.get("type") == "column":
                out.add(n["source"])
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(node)
    return out - own


def numbers(db, schema) -> set:
    """The (table, column) ids the view types as numbers that the database stores as TEXT: an upload's cells
    (pipeline.cell_type)."""
    out = set()
    for t, d in defs(schema).items():
        stored = {n: typ.upper() for _, n, typ, *_ in db.execute(f'PRAGMA table_info("{d["sql_name"]}")')}
        out |= {(t, c["column_id"]) for c in d["columns"]
                if c["sql_type"] in ("INTEGER", "REAL") and stored.get(c["sql_name"]) == "TEXT"}
    return out


def typed(node, numbers: set, aliases: dict):
    """Each column of `numbers` the node reads as a value, read as a number: REAL(NULLIF(TRIM(cell), '')) (sorted
    and totalled as numbers, an empty cell NULL); aliases: alias → table id, the node's own added. A value already
    read so (an update's, pipeline.reader) is left as is."""
    found = dict(aliases)

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "database_table" and "alias" in n:
                found[n["alias"]] = n["reference"]
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(node)

    def put(n):
        if isinstance(n, list):
            return [put(v) for v in n]
        if not isinstance(n, dict) or n.get("type") == "function" and n.get("name") == "real":
            return n
        if n.get("type") == "column" and (found.get(n["source"]), n["reference"]) in numbers:
            return {"type": "function", "name": "real", "arguments": [{"type": "function", "name": "nullif", "arguments": [
                {"type": "function", "name": "trim", "arguments": [n]}, {"type": "literal", "value": ""}]}]}
        return {k: put(v) for k, v in n.items()}
    return put(node)


class Names:
    """Aliases and subquery ids, unique within one statement."""

    def __init__(self, schema):
        self.d, self.used, self.q = defs(schema), set(), 0

    def alias(self, table):
        base = (self.d.get(table, {}).get("sql_name") or table.removeprefix("t_"))[:1] or "t"
        a, i = base, 1
        while a in self.used:
            i += 1
            a = f"{base}{i}"
        self.used.add(a)
        return a

    def qid(self):
        self.q += 1
        return f"q{self.q}"


def select(names, table, alias, columns, where=None, limit=None):
    return {"id": names.qid(), "type": "select", "from": {"type": "database_table", "reference": table, "alias": alias},
            "columns": [{"expression": c, "alias": f"c{i}"} for i, c in enumerate(columns)],
            "scope": {"type": "filtered", "where": where} if where else {"type": "all_rows"},
            **({"limit": limit} if limit is not None else {})}


def lift(cond, main, main_alias, schema, names, warnings, rid):
    """A condition tree on the request's tables → on the main table: a leaf on another table becomes a chain of
    IN (SELECT …) along the foreign keys (each leaf on its own: for many-to-one links, the same as one join)."""
    if cond is None:
        return None
    if cond.get("type") in ("all", "any"):
        kids = [lift(c, main, main_alias, schema, names, warnings, rid) for c in cond["children"]]
        kids = [k for k in kids if k is not None]
        return kids[0] if len(kids) == 1 else {"type": cond["type"], "children": kids} if kids else None
    if cond.get("type") == "not" and tables_in(cond) - {main}:
        inner = lift(cond["operand"], main, main_alias, schema, names, warnings, rid)
        return inner and {"type": "not", "operand": inner}
    tables = tables_in(cond)
    if tables <= {main}:
        return rewrite(cond, {main: main_alias})
    if len(tables) > 1:
        warnings.append({"request": rid, "problem": "a condition comparing two tables: not supported yet",
                         "tables": sorted(tables)})
        return None
    (other,) = tables
    chain = path(schema, main, other)
    if chain is None:
        warnings.append({"request": rid, "problem": f"no foreign-key path from {main} to {other}"})
        return None
    inner = None
    for i in range(len(chain) - 1, -1, -1):           # from the far table back to the main one
        t_from, c_from, t_to, c_to, _ = chain[i]
        a_to = names.alias(t_to)
        where = rewrite(cond, {other: a_to}) if i == len(chain) - 1 else inner
        q = select(names, t_to, a_to, [col(a_to, c_to)], where)
        a_from = main_alias if i == 0 else None
        inner = {"type": "membership", "operator": "in", "expression": col(a_from or f"__{t_from}", c_from), "query": q}
    # the outer column of each level is the alias of the level above: fill them from the main table down
    return _fill_outer(inner, main_alias)


def _fill_outer(node, outer_alias):
    """Each membership's expression column waits for the alias of the select around it."""
    expr = node["expression"]
    if expr["source"].startswith("__"):
        expr["source"] = outer_alias
    q = node["query"]
    w = q["scope"].get("where")
    if w and w.get("type") == "membership" and w["expression"]["source"].startswith("__"):
        _fill_outer(w, q["from"]["alias"])
    return node


def filled(node, table) -> list:
    """An update's value from an upload: each of its cells there not empty, TRIM(COALESCE(cell, '')) <> '' (an
    empty cell is no value: never written over the store's)."""
    cells = []

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "column" and n["source"] == table and n not in cells:
                cells.append(n)
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(node)
    return [{"type": "comparison", "operator": "!=", "right": {"type": "literal", "value": ""}, "left": {
        "type": "function", "name": "trim", "arguments": [
            {"type": "function", "name": "coalesce", "arguments": [c, {"type": "literal", "value": ""}]}]}}
            for c in cells]


def shown(key, main, main_alias, schema, names, warnings, rid, expression=None, also=()):
    """A column to show or sort by: on the main table, or a one-row subquery along many-to-one keys. expression: an
    expression on that column's table (an update's value from another table) taken in its place; also: conditions
    on that table its row meets too."""
    table, cid = key.split(".")
    if table == main:
        return col(main_alias, cid)
    chain = path(schema, main, table)
    if chain is None or any(e[4] != "up" for e in chain):
        warnings.append({"request": rid, "problem": f"{key}: several values per record (one-to-many): not shown"})
        return None
    value = col(main_alias, chain[0][1])
    for i, (t_from, c_from, t_to, c_to, _) in enumerate(chain):
        a = names.alias(t_to)
        out = col(a, chain[i + 1][1]) if i + 1 < len(chain) else \
            rewrite(expression, {table: a}) if expression else col(a, cid)
        where = {"type": "comparison", "operator": "=", "left": col(a, c_to), "right": value}
        if also and i + 1 == len(chain):
            where = {"type": "all", "children": [where, *rewrite(list(also), {table: a})]}
        value = {"type": "subquery", "query": select(names, t_to, a, [out], where, 1)}
    return value


def totalled(output, main, main_alias, schema, names):
    """An aggregate of a column on a table whose rows point to the main table's record (one-to-many: an upload's
    rows per sku and warehouse), one total per record: a correlated subquery; None for any other path."""
    table, cid = output["column"].split(".")
    chain = path(schema, main, table)
    if not chain or len(chain) != 1 or chain[0][4] != "down":
        return None
    _, c_from, _, c_to, _ = chain[0]
    b = names.alias(table)
    where = {"type": "comparison", "operator": "=", "left": col(b, c_to), "right": col(main_alias, c_from)}
    return {"type": "subquery", "query": select(names, table, b, [{**output["expression"], "argument": col(b, cid)}],
                                                where)}


# ---- statements -----------------------------------------------------------------------------------------------

def main_of(requests, sources, parts, rid):
    """The stored table a request works on, or the table an earlier request creates."""
    t = parts_main_table(requests, sources, rid)
    if t:
        return t
    node = sources.get(rid, {}).get("target") or sources.get(rid, {}).get("from") or {}
    if node.get("type") == "new_table":
        create = parts.get(node["request_id"], {}).get("create")
        return create and create["table_id"]
    return None


def statement(r, st, main, schema, src, part, cond6, group6, built, warnings, numbers=frozenset()):
    """One request's AST, or None (with a warning); numbers: the columns read as numbers (typed): a read's and a
    condition's, never an update's value (read by pipeline.reader) or an insert's."""
    rid, names = r["id"], Names(schema)
    if st == "drop_table":
        return {"id": rid, "type": "drop_table", "target": {"type": "database_table", "reference": main}}
    if st == "alter_table":
        if not part.get("actions"):
            return None
        return {"id": rid, "type": "alter_table", "target": {"type": "database_table", "reference": main},
                "actions": part["actions"]}
    if st in ("create_table", "insert") and (src.get("query_from") or {}).get("type") in ("database_table",
                                                                                        "request_result", "new_table"):
        warnings.append({"request": rid, "problem": "copying existing records is not built yet: the table would get "
                                                    "no copied rows"})      # Codex review, bug 4: never silent
    if st == "create_table":
        return part.get("create") and {"id": rid, "type": "create_table", "definition": part["create"]}
    a = names.alias(main)
    target = {"type": "database_table", "reference": main, "alias": a}
    if st == "insert":
        ins = part.get("insert") or {}
        if not ins.get("columns"):
            return None
        if frm := ins.get("from"):                      # an upload's new records: a key not empty, not in the store
            b, s = names.alias(frm), names.alias(main)
            key, known = col(b, ins["key"]), col(s, ins["store_key"])
            new = {"type": "all", "children": [
                {"type": "null_check", "expression": key, "operator": "is_not_null"},
                {"type": "comparison", "operator": "!=", "left": key, "right": {"type": "literal", "value": ""}},
                {"type": "membership", "operator": "not_in", "expression": key, "query": select(
                    names, main, s, [known], {"type": "null_check", "expression": known, "operator": "is_not_null"})}]}
            return {"id": rid, "type": "insert", "target": target, "columns": ins["columns"],
                    "query": select(names, frm, b, [rewrite(v, {frm: b}) for v in ins["values"]], new)}
        return {"id": rid, "type": "insert", "target": target, "columns": ins["columns"], "values": [ins["values"]]}
    where = typed(lift((group6 or {}).get("where"), main, a, schema, names, warnings, rid), numbers, {a: main})
    scope = {"type": "filtered", "where": where} if where else {"type": "all_rows"}
    if st == "update":
        part = {**part, "assignments": [x for x in part.get("assignments", []) if not x.get("pending")]}
        if not part.get("assignments"):                 # a value still pending (formula not run): never SET … NULL
            return None
        def cases(v):                                   # a CASE's condition (step 5's formula) is lifted like WHERE
            if isinstance(v, list):
                return [cases(x) for x in v]
            if not isinstance(v, dict):
                return v
            if v.get("type") == "case":
                return {**v, "branches": [{"when": lift(b["when"], main, a, schema, names, warnings, rid),
                                           "then": cases(b["then"])} for b in v["branches"]], "else": cases(v["else"])}
            return {k: cases(x) for k, x in v.items()}
        sets, reached = [], {}
        for x in part["assignments"]:
            v = rewrite(cases(x["value"]), {main: a})
            if x["value"].get("type") != "case" and (others := tables_in(x["value"]) - {main}):
                (other,) = others if len(others) == 1 else (None,)  # a value from another table (an upload's
                also = filled(x["value"], other) if other and x.get("from") == "incoming" else []   # price): its
                v = other and shown(f"{other}.", main, a, schema, names, warnings, rid, v, also)    # subquery
                if v is None:
                    warnings.append({"request": rid, "problem": f"value of {x['column']}: not one value per record"})
                    continue
                got = reached.setdefault(other, [])
                got += [c for c in also if c not in got]
            sets.append({"column": col(a, x["column"].split(".")[1]), "value": v})
        if not sets:
            return None
        ast = {"id": rid, "type": "update", "target": target, "scope": scope, "assignments": sets}
        for other, also in reached.items():            # only the records with a value there: never SET … NULL
            key = path(schema, main, other)[-1][3]     # (an upload's row with every value cell of it filled)
            has = {"type": "null_check", "expression": col(other, key), "operator": "is_not_null"}
            with_condition(ast, lift({"type": "all", "children": [has, *also]} if also else has,
                                     main, a, schema, names, warnings, rid))
        return ast
    if st == "delete":
        return {"id": rid, "type": "delete", "target": target, "scope": scope}
    # select
    outs = part.get("outputs")
    grouped = {f"{g['source']}.{g['reference']}" for g in (group6 or {}).get("group_by", [])}
    if isinstance(outs, list):                          # a grouping column is shown as itself: "count per brand"
        outs = [o | {"aggregate": "value", "expression": col(o["column"].split(".")[0], o["column"].split(".")[1])}
                if o["column"] in grouped else o for o in outs]
    columns = []
    for g in (group6 or {}).get("group_by", []):        # "per brand": the group's column is shown first
        key = f"{g['source']}.{g['reference']}"
        if not (isinstance(outs, list) and any(o["column"] == key and o["aggregate"] == "value" for o in outs)):
            if (v := shown(key, main, a, schema, names, warnings, rid)) is not None:
                columns.append({"expression": v, "alias": g["reference"].removeprefix("c_")})
    if outs == "all" or not outs:
        columns += [{"expression": col(a, c["column_id"]), "alias": c["sql_name"]} for c in defs(schema)[main]["columns"]]
    else:
        for o in outs:
            if o["column"] is None:                         # count of records
                columns.append({"expression": {"type": "aggregate", "function": "count"}, "alias": "count"})
                continue
            if o["aggregate"] != "value" and (e := totalled(o, main, a, schema, names)):
                columns.append({"expression": e, "alias": f"{o['aggregate']}_{o['column'].split('.')[1][2:]}"})
                continue
            v = shown(o["column"], main, a, schema, names, warnings, rid)
            if v is None:
                continue
            e = v if o["aggregate"] == "value" else {**o["expression"], "argument": v}
            columns.append({"expression": e, "alias": f"{o['aggregate']}_{o['column'].split('.')[1][2:]}"
                            if o["aggregate"] != "value" else o["column"].split(".")[1][2:]})
    if not columns:
        return None
    sel = {"id": rid, "type": "select", "from": target, "scope": scope, "columns": columns}
    if group6:
        if group6.get("group_by"):
            sel["group_by"] = [g for g in (shown(f"{x['source']}.{x['reference']}", main, a, schema, names, warnings, rid)
                                           for x in group6["group_by"]) if g is not None]
        order = [{"expression": e, "direction": o["direction"]} for o in group6.get("order_by", [])
                 if (e := shown(f"{o['expression']['source']}.{o['expression']['reference']}", main, a, schema, names,
                                warnings, rid)) is not None]
        if order:
            sel["order_by"] = order
        if group6.get("limit") is not None:
            sel["limit"] = group6["limit"]
    return typed(sel, numbers, {})


def from_rows(ast, earlier, main, schema):
    """ "those" after a select: the main table's id IN (that select, showing only the id, same conditions, order,
    limit). After an update or delete the ids come at run time (RETURNING), see execute()."""
    k = pk(schema, main)
    sub = json.loads(json.dumps(earlier))
    renamed = {}

    def rename(n):                                       # its own aliases inside the later statement: v → v_r1
        if isinstance(n, dict):
            if n.get("type") == "database_table" and "alias" in n:
                renamed[n["alias"]] = n["alias"] = f"{n['alias']}_{earlier['id']}"
            for v in n.values():
                rename(v)
        elif isinstance(n, list):
            for v in n:
                rename(v)
    rename(sub)
    sub = rewrite(sub, renamed)
    alias = sub["from"]["alias"]
    sub.update(id=f"{sub['id']}_rows", columns=[{"expression": col(alias, k), "alias": "k"}])
    sub.pop("group_by", None)
    member = {"type": "membership", "operator": "in", "expression": col(ast["target" if "target" in ast else "from"]["alias"], k),
              "query": sub}
    return member


def grouped(select: dict) -> bool:
    """A read whose rows are groups or totals ("the number of products per brand"), not records with an id."""
    return bool(select.get("group_by") or select.get("having") or
                any(c["expression"].get("type") == "aggregate" for c in select.get("columns", [])))

IDS = "__rows_of_{}__"                                       # replaced by the ids the session passes


def with_condition(ast, cond):
    key = "scope"
    w = ast[key].get("where")
    ast[key] = {"type": "filtered", "where": cond if not w else {"type": "all", "children": [cond, w]}}
    return ast


# ---- run ------------------------------------------------------------------------------------------------------

def compiled(db, schema, items) -> list[dict]:
    """Every statement's SQL, in order, prepared by SQLite (sql.check: no row read, nothing run). A request reading
    the rows an insert, update or delete changed reads their ids from that statement's RETURNING, which the session
    passes when it runs the program (:rows_<request>, a JSON list): the ids come from the execution only."""
    errors = []
    for i, it in enumerate(items):
        if it.get("rows_from"):                             # affected rows: the ids the execution returns
            alias = it["ast"]["target" if "target" in it["ast"] else "from"]["alias"]
            with_condition(it["ast"], {"type": "membership", "operator": "in", "expression": col(alias, pk(schema, it["main"])),
                                       "values": [{"type": "literal", "value": IDS.format(it["rows_from"])}]})
        try:
            before = len(program([x["ast"] for x in items[:i]], schema)) if i else 0
            it["sql"] = program([x["ast"] for x in items[:i + 1]], schema)[before:]
        except Exception as e:                              # the AST is refused: nothing runs after it
            errors.append({"request": it["request"], "stage": "compile", "error": str(e)[:300]})
            break
        if it.get("rows_from"):
            it["sql"] = [q.replace(f"'{IDS.format(it['rows_from'])}'", f"SELECT value FROM json_each(:rows_{it['rows_from']})")
                         for q in it["sql"]]
        if it.get("returning"):
            it["sql"] = [f'{q} RETURNING "{it["returning"]}"' for q in it["sql"]]
    sql = [q for it in items if "sql" in it for q in it["sql"]]
    if not errors and (bad := check(db, sql)):
        errors.append({"request": next(it["request"] for it in items if bad[0]["sql"] in it.get("sql", [])),
                       "stage": "sqlite", "error": bad[0]["error"][:300]})
    return errors


def run_order(requests: list[dict], order: list[str] | None = None) -> list[dict]:
    """Step 1's execution order (each request after the one it waits for: "show those, after you lower …" runs the
    change first); without it, the same order from the requests' waits_for, else as written."""
    by_id = {r["id"]: r for r in requests}
    if order and set(order) == set(by_id):
        return [by_id[i] for i in order]
    done = []
    while len(done) < len(requests):
        nxt = next((r for r in requests if r not in done and (not r.get("waits_for") or by_id.get(r["waits_for"]) in done
                                                             or r["waits_for"] not in by_id)), None)
        done.append(nxt or next(r for r in requests if r not in done))      # a cycle: written order, never stuck
    return done


def assemble(db, schema, requests, operation, sources, parts, conditions, grouping, steps_warnings=(),
             order: list[str] | None = None) -> dict:
    """requests: step 1's; operation, sources, parts, conditions, grouping: each step's section["requests"];
    order: step 1's execution order."""
    warnings, items = [], []
    store_name = {t: d["sql_name"] for t, d in defs(schema).items()}
    typed_columns = numbers(db, schema)
    for r in run_order(requests, order):
        rid = r["id"]
        st = operation.get(rid, {}).get("statement")
        main = main_of(requests, sources, parts, rid)
        if st not in ("select", "update", "delete", "insert", "alter_table", "create_table", "drop_table") or \
                (not main and st != "create_table"):
            warnings.append({"request": rid, "problem": f"not built: statement {st}, table {main}"})
            continue
        g6, c6 = grouping.get(rid, {}), conditions.get(rid, {})
        ast = statement(r, st, main, schema, sources.get(rid, {}), parts.get(rid, {}), c6, g6, items, warnings,
                        typed_columns)
        if ast is None:
            warnings.append({"request": rid, "problem": "not built: a part is missing"})
            continue
        it = {"request": rid, "ast": ast, "main": main}
        frm = g6.get("rows_from") or c6.get("rows_from")
        if frm and st in ROWS:
            producer = next((x for x in items if x["request"] == frm), None)
            unlinked = None                                  # why "those" cannot be linked: then nothing is built
            if producer is None:
                unlinked = f"takes the rows of {frm}, which was not built"
            elif producer["ast"]["type"] == "select" and grouped(producer["ast"]):
                unlinked = (f"takes the rows of {frm}, which are groups or totals, not records: which records are "
                            f"meant?")                        # Codex review, bug 5
            elif producer["ast"]["type"] == "select":
                if producer["main"] == main:
                    with_condition(ast, from_rows(ast, producer["ast"], main, schema))
                else:
                    unlinked = f"the rows of {frm} are another table's: not linked yet"
            elif producer["ast"]["type"] in ("insert", "update", "delete"):   # the rows it adds or changes: their ids
                if producer["main"] == main:
                    producer["returning"] = next(c["sql_name"] for c in defs(schema)[main]["columns"]
                                                 if c["column_id"] == pk(schema, main))
                    it["rows_from"] = frm
                    if producer["ast"]["type"] == "delete":
                        warnings.append({"request": rid, "problem": f"takes the rows {frm} deletes: they no longer exist"})
                else:
                    unlinked = f"the rows {frm} changed are another table's: not linked yet"
            else:
                unlinked = f"takes the rows of {frm} ({producer['ast']['type']}): not linked yet"
            if unlinked:                                     # never every record instead of "those": asked, not built
                warnings.append({"request": rid, "problem": f"not built: {unlinked}"})
                continue
        items.append(it)
    errors = compiled(db, schema, items)
    for it in items:                                    # what the merchant should see before approving
        a = it["ast"]
        if a["type"] in ("update", "delete") and a["scope"]["type"] == "all_rows":
            warnings.append({"request": it["request"], "problem": f"changes every record of {store_name.get(it['main'])}",
                             "scope": "all_rows"})          # read by the session: asked first
    questions = [{"step": s, **w} for s, ws in steps_warnings for w in ws] + \
        [{"step": "assembly", **w} for w in warnings] + [{"step": "assembly", **e} for e in errors]
    return {"program": [{k: v for k, v in it.items() if k != "main"} for it in items],
            "questions": questions, "errors": errors}
