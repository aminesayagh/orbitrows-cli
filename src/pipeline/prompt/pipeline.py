"""One prompt → a checked SQL program on the store's tables: steps 1 to 7 of the PoC (v2/prompt_ast/pipeline.py), in
its order, each step fed the previous steps' outputs. Paid: the model; local: CorPipe and GLiNER2.

    store   the store's tables read back from SQLite (ddl.schema_from_sqlite), without the CLI's own tables (`_…`);
            with the selected uploads' tables, keyed and linked in that view only (with_incoming): an update takes
            a linked upload column's value through the key, converted by the user's formula (from_incoming), or
            the upload column a step names (its type the one its cells show: cell_type; read the same: from_uploads);
            an insert takes the upload's records whose key the store doesn't have (insert_from)
    out     {prompt, requests, operation, values, sources, parts, conditions, grouping, formula, assembly}; assembly is
            {"program", "questions", "errors"}: step 7 runs the program in a savepoint, rolled back (nothing is changed)

A model answer below a step's threshold is a question for the user (asking.py): run stops after the first step that
has one (Paused, as the store import); once answered (client.settle), it runs again from the start, replaying the
recorded answers (client.Recorded). Every other warning is a notice: shown, and the pipeline goes on.
"""
import re

from orbitrows.pipeline.merge import convert
from orbitrows.pipeline.store.run import Paused, _Ordered, first_questions
from orbitrows.services.extractor import client as extractor
from orbitrows.services.storage.store import ROLES
from orbitrows.sql.ddl import schema_from_sqlite

from . import assemble, conditions, coref, formula, grouping, operation, parts, request_graph, source_route, sources, values

NUMBER = re.compile(r"[+-]?([0-9]+(\.[0-9]*)?|\.[0-9]+)")    # an upload's number cell: a . for decimals, no unit
WHOLE = re.compile(r"[+-]?[0-9]+(\.0*)?")                     # into an INTEGER column without a formula


def store_of(db) -> list[dict]:
    """The store's create_table nodes, as the PoC's Orbitrow schema(); `_` tables and columns are the CLI's
    (classification, history; a file's row order), not the merchant's."""
    known = {name for (name,) in db.execute("SELECT sql_name FROM _store_tables")} if db.execute(
        "SELECT 1 FROM sqlite_schema WHERE name = '_store_tables'").fetchone() else None
    nodes = [n for n in schema_from_sqlite(db) if not n.definition.sql_name.startswith("_")
             and (known is None or n.definition.sql_name in known)]
    for n in nodes:
        n.definition.columns = [c for c in n.definition.columns if not c.sql_name.startswith("_")]
    return [n.model_dump() for n in nodes]


def with_incoming(db, store: list[dict], incoming: list[dict]) -> tuple[list[dict], str | None]:
    """The store's nodes and each selected upload's, linked in this view only (never in the database): the upload's
    key is its primary_key_candidate column, and each store key directly matched to it (its table's primary key or a
    dedicated_key column) references it, so a store record reaches the upload's row along a many-to-one ("up") key.
    → (schema, None), or (store, why) when an upload has no such key link: never guessed."""
    by_name = {n.definition.sql_name: n.model_dump() for n in schema_from_sqlite(db)}
    tables = {n["definition"]["sql_name"]: n["definition"] for n in store}
    out = list(store)
    for t in incoming:
        links = keyed(db, t["id"], tables)
        if not links:
            return store, (f"{t['sql_name']} ({t['source_file']}) has no key column linked to a store key (a table's "
                           "primary key or reference code), so its rows can't be matched to the store's records; "
                           "nothing is guessed. Reading an upload without such a link is not implemented yet.")
        node = by_name[t["sql_name"]]
        node["definition"]["primary_key"] = [f"c_{links[0][0]}"]
        for c in node["definition"]["columns"]:               # a key keeps its type: as compared with the store's
            if c["sql_name"] not in {k for k, *_ in links}:
                c["sql_type"] = cell_type(db, t["sql_name"], c["sql_name"])
        for key, table, column in links:
            tables[table]["foreign_keys"] = tables[table].get("foreign_keys", []) + [
                {"columns": [f"c_{column}"], "references": {"table": node["definition"]["table_id"],
                                                            "columns": [f"c_{key}"]}}]
        out.append(node)
    return out, None


def cell_type(db, table: str, column: str) -> str:
    """An upload column's type as its cells show it (every cell is TEXT in the database: a raw snapshot): INTEGER
    when each non-empty cell is a whole number, REAL when each is a plain number, else TEXT (an empty column too).
    The view's only: a step may then name it for a number change; its value is cast when read (from_uploads)."""
    cells = [str(v).strip() for (v,) in db.execute(f'SELECT DISTINCT "{column}" FROM "{table}" '
                                                   f'WHERE TRIM(COALESCE("{column}", \'\')) <> \'\'')]
    if not cells or not all(NUMBER.fullmatch(v) for v in cells):
        return "TEXT"
    return "INTEGER" if all(WHOLE.fullmatch(v) for v in cells) else "REAL"


def keyed(db, upload: int, tables: dict) -> list[tuple]:
    """The upload's key links [(its key column, store table, store column)]: a primary_key_candidate column directly
    matched to a store key (its table's primary key or a dedicated_key column). The update (with_incoming) and the
    insert (insert_from) read the same ones."""
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE name = '_incoming_store_matches'").fetchone():
        return []
    links = db.execute(
        "SELECT ic.sql_name, st.sql_name, sc.sql_name, sc.role FROM _incoming_columns ic "
        "JOIN _incoming_store_matches m ON m.incoming_column_id = ic.id AND m.relation = 'direct' "
        "JOIN _store_columns sc ON sc.id = m.store_column_id JOIN _store_tables st ON st.id = sc.table_id "
        "WHERE ic.table_id = ? AND json_extract(ic.extra_json, '$.primary_key_candidate')", (upload,)).fetchall()
    return [(key, table, column) for key, table, column, role in links if table in tables and (
        role == ROLES.index("dedicated_key") or tables[table]["primary_key"] == [f"c_{column}"])]


def repeated(db, table: str, key: str) -> str | None:
    """Why the upload can't be matched on `key`: a key value on several rows (the store's tables don't enforce
    their keys, so an insert would add the record twice and an update would take one row of them), or None."""
    found = [v for (v,) in db.execute(f'SELECT "{key}" FROM "{table}" WHERE TRIM(COALESCE("{key}", \'\')) <> \'\' '
                                      f'GROUP BY "{key}" HAVING COUNT(*) > 1 LIMIT 3')]
    return found and (f"{key} of {table} repeats {', '.join(map(repr, found))}: each record's key must be on one row "
                      "only, so its rows can't be matched to the store's records; nothing is guessed. Remove the "
                      "repeated rows, then upload it again.") or None


def _put(node, value):
    """The formula's AST with its x (convert.X) replaced by the upload's value."""
    if node == convert.X.model_dump(by_alias=True):
        return value
    if isinstance(node, dict):
        return {k: _put(v, value) for k, v in node.items()}
    if isinstance(node, list):
        return [_put(v, value) for v in node]
    return node


def reader(db, store: list[dict], incoming: list[dict], asked: list, errors: list):
    """taken(rid, upload, i, s, name, typ): upload column i's value (its sql name `name`) for store column s (typ
    its type), checked and converted; None when asked (a formula to give, into `asked`) or refused (into `errors`:
    a unit varying row by row, a key repeated, a number cell that isn't a plain number). Every step's value read from
    an upload goes through it, whichever step chose that column."""
    names = {t["id"]: t["sql_name"] for t in incoming}
    tables = {n["definition"]["sql_name"]: n["definition"] for n in store}
    keys = {t["id"]: keyed(db, t["id"], tables) for t in incoming}
    checked = {}                                              # upload → why its keys repeat, or None: checked once

    def taken(rid, upload, i, s, name, typ):
        """Upload column i's value for store column s, converted; None when asked or refused."""
        plan = convert.plan(db, upload, (i, s))              # its pair's units, linked or not
        if upload not in checked:
            checked[upload] = next(filter(None, (repeated(db, names[upload], k) for k in dict.fromkeys(
                k for k, *_ in keys[upload]))), None)
            if checked[upload]:
                errors.append({"request": rid, "stage": "key", "error": checked[upload]})
        if checked[upload]:
            return None
        cell = {"type": "column", "source": f"t_{names[upload]}", "reference": f"c_{name}"}
        value = {"type": "function", "name": "nullif", "arguments": [
            {"type": "function", "name": "trim", "arguments": [cell]}, {"type": "literal", "value": ""}]}
        if typ in ("INTEGER", "REAL"):
            value = {"type": "function", "name": "real", "arguments": [value]}
        if refused := next((x for x in plan["refused"] if x["pair"] == (i, s)), None):
            errors.append({"request": rid, "stage": "conversion", "error": refused["reason"]})
            return None
        if need := next((x for x in plan["needs"] if x["pair"] == (i, s)), None):
            q = convert.question(db, need)
            asked.append({"request": rid} | q | {"question": q["question"] | {
                "save": [s, need["from"], need["to"]]}})         # the session keeps the answer (convert.save)
            return None
        formula = next((x["formula"] for x in plan["known"] if x["pair"] == (i, s)), None)
        if typ in ("INTEGER", "REAL"):                        # CAST('$5' AS REAL) is 0: checked here, never guessed
            whole = typ == "INTEGER" and not formula          # 12.5 into an INTEGER column: refused, not rounded
            bad = [v for (v,) in db.execute(f'SELECT DISTINCT TRIM("{name}") FROM "{names[upload]}" '
                                            f'WHERE TRIM(COALESCE("{name}", \'\')) <> \'\'')
                   if not (WHOLE if whole else NUMBER).fullmatch(v)]
            if bad:
                errors.append({"request": rid, "stage": "conversion", "error": (
                    f"{name} of {names[upload]} has cells that aren't plain {'whole ' * whole}numbers "
                    f"({', '.join(map(repr, bad[:3]))}), and its store column takes "
                    f"{'whole numbers' if whole else 'numbers'}: nothing is guessed. Write them as "
                    f"{'12' if whole else '12.5 (a . for decimals, no unit)'}, then upload it again.")})
                return None
        if formula:
            value = _put(convert.parse(formula).model_dump(by_alias=True), value)
            if typ == "INTEGER":                              # the formula's result: rounded to the whole number
                value = {"type": "function", "name": "round", "arguments": [value]}
        return value

    return taken


def from_incoming(db, store: list[dict], incoming: list[dict], reqs: list[dict], parts_: dict,
                  sources_: dict) -> tuple[list, list]:
    """An update's changed column directly matched to a (non-key) column of a selected upload takes that column's
    value, which assembly reaches through the key: as is (an empty cell is NULL, never written over a value; cast to
    REAL for a number column: incoming cells are TEXT), or converted by the user's formula (convert.py). → (questions:
    a formula to give, errors: a unit varying row by row, a key repeated, a number cell that isn't a plain number);
    each assignment so built is set (from: incoming), no longer pending. An insert takes the upload's new records
    (insert_from)."""
    asked, errors = [], []
    plans = {t["id"]: convert.plan(db, t["id"]) for t in incoming}
    names = {t["id"]: t["sql_name"] for t in incoming}
    taken = reader(db, store, incoming, asked, errors)
    types = {f"{n['definition']['table_id']}.{c['column_id']}": c["sql_type"] for n in store
             for c in n["definition"]["columns"]}
    tables = {n["definition"]["sql_name"]: n["definition"] for n in store}
    keys = {t["id"]: keyed(db, t["id"], tables) for t in incoming}
    for r in reqs:
        if r["statement"] == "insert":
            insert_from(db, r["id"], parts.main_table(reqs, sources_, r["id"]), plans, names, types, taken,
                        parts_.setdefault(r["id"], {}), errors, keys)
        if r["statement"] != "update":
            continue
        for a in parts_.get(r["id"], {}).get("assignments", []):
            table, column = (x.split("_", 1)[1] for x in a["column"].split("."))
            linked = [x for x in db.execute(
                "SELECT ic.table_id, ic.id, sc.id, ic.sql_name FROM _incoming_store_matches m "
                "JOIN _incoming_columns ic ON ic.id = m.incoming_column_id "
                "JOIN _store_columns sc ON sc.id = m.store_column_id JOIN _store_tables st ON st.id = sc.table_id "
                "WHERE m.relation = 'direct' AND st.sql_name = ? AND sc.sql_name = ? "
                "AND NOT COALESCE(json_extract(ic.extra_json, '$.primary_key_candidate'), 0) "
                "ORDER BY ic.table_id, ic.id", (table, column)) if x[0] in plans]
            if not linked:
                continue
            upload, i, s, name = linked[0]
            if (value := taken(r["id"], upload, i, s, name, types[a["column"]])) is None:
                continue
            a.update({"value": value, "from": "incoming"})
            a.pop("pending", None)
    return asked, errors


def reads(value, incoming: list[dict]) -> bool:
    """Does the value read a column of a selected upload?"""
    if isinstance(value, dict):
        return value.get("type") == "column" and value.get("source") in {f"t_{t['sql_name']}" for t in incoming} \
            or any(reads(v, incoming) for v in value.values())
    return isinstance(value, list) and any(reads(v, incoming) for v in value)


def from_uploads(db, store: list[dict], incoming: list[dict], reqs: list[dict], parts_: dict) -> tuple[list, list]:
    """An update's value that reads an upload's column, whichever step chose it (step 5's parts, step 9's formula:
    "set the price to the recommended price of supplier_offer.csv"), each cell read as a link's is (reader): trimmed,
    an empty one NULL and no write (assembly, from: incoming), cast for a number column, converted by the units of
    that (upload column, store column) pair. A value from a saved link (from_incoming) is not read twice.
    → (questions, errors), as from_incoming's."""
    asked, errors = [], []
    taken = reader(db, store, incoming, asked, errors)
    ids = {f"t_{t['sql_name']}": t["id"] for t in incoming}
    types = {f"{n['definition']['table_id']}.{c['column_id']}": c["sql_type"] for n in store
             for c in n["definition"]["columns"]}
    for r in reqs:
        for a in (parts_.get(r["id"]) or {}).get("assignments", []) if r["statement"] == "update" else []:
            if a.get("from") == "incoming" or a.get("pending") or not reads(a["value"], incoming):
                continue
            table, column = (x.split("_", 1)[1] for x in a["column"].split("."))
            s = db.execute("SELECT sc.id FROM _store_columns sc JOIN _store_tables st ON st.id = sc.table_id "
                           "WHERE st.sql_name = ? AND sc.sql_name = ?", (table, column)).fetchone()
            typ = types[a["column"]]
            if a["value"].get("type") != "column" and typ == "INTEGER":   # within a formula, rounded by it: a
                typ = "REAL"                                              # 12.5 cell is no longer refused
            failed = []

            def put(n):
                if isinstance(n, list):
                    return [put(v) for v in n]
                if not isinstance(n, dict):
                    return n
                if n.get("type") == "column" and n.get("source") in ids:
                    upload, name = ids[n["source"]], n["reference"].removeprefix("c_")
                    (i,) = db.execute("SELECT id FROM _incoming_columns WHERE table_id = ? AND sql_name = ?",
                                      (upload, name)).fetchone()
                    if (v := taken(r["id"], upload, i, s and s[0], name, typ)) is None:
                        failed.append(n)
                    return v
                return {k: put(v) for k, v in n.items()}
            value = put(a["value"])
            if not failed:
                a.update({"value": value, "from": "incoming"})
    return asked, errors


def insert_from(db, rid, main, plans, names, types, taken, part, errors, keys):
    """An insert into `main` from the first selected upload whose key is linked to it (keys: keyed()'s links, as
    with_incoming chose them): the store's columns directly
    linked to that upload (the key included; only those step 5 says the request gives, when it names some), each
    value converted (taken); assembly reads the upload's rows whose key is not empty and not in the store yet. Never
    guessed: a NOT NULL store column without default and without a link, or a linked column of a parent table (the
    record would need one there), stops the message."""
    table = main and main.split("_", 1)[1]
    links = [x for x in db.execute(
        "SELECT ic.table_id, ic.id, sc.id, ic.sql_name, sc.sql_name, st.sql_name FROM _incoming_store_matches m "
        "JOIN _incoming_columns ic ON ic.id = m.incoming_column_id "
        "JOIN _store_columns sc ON sc.id = m.store_column_id JOIN _store_tables st ON st.id = sc.table_id "
        "WHERE m.relation = 'direct' ORDER BY ic.table_id, ic.id") if x[0] in plans]
    is_key = lambda x: x[5] == table and (x[3], x[5], x[4]) in keys[x[0]]
    upload = next((x[0] for x in links if is_key(x)), None)
    if upload is None:
        errors.append({"request": rid, "stage": "insert", "error": f"no selected upload has a key linked to {table}: "
                                                                   "its new records can't be told; nothing is guessed."})
        return
    parents = {p for (_, _, p, *_) in db.execute(f'PRAGMA foreign_key_list("{table}")')}
    if split := sorted({f"{x[5]}.{x[4]}" for x in links if x[0] == upload and x[5] in parents}):
        errors.append({"request": rid, "stage": "insert", "error": f"{', '.join(split)} live in another table: a new "
                       f"{table} record would need its record there first, which is not built yet."})
        return
    gives = set((part.get("insert") or {}).get("gives") or [])
    key = next(x for x in links if x[0] == upload and is_key(x))
    mine = {}                                                 # store column → its first linked upload column
    for x in links:
        if x[0] == upload and x[5] == table and x[4] not in mine and (
                x is key or not gives or f"t_{table}.c_{x[4]}" in gives):
            mine[x[4]] = x
    mine = {key[4]: key} | mine
    required = [n for _, n, typ, nn, default, pk in db.execute(f'PRAGMA table_info("{table}")')
                if (nn or pk and typ.upper() != "INTEGER") and default is None and n not in mine]
    if required:
        errors.append({"request": rid, "stage": "insert", "error": f"{', '.join(required)} of {table} must have a "
                       f"value and {names[upload]} has no column linked to {'it' if len(required) == 1 else 'them'}: "
                       "nothing is guessed."})
        return
    cols, vals = [], []
    for column, (_, i, s, name, *_) in mine.items():
        if (value := taken(rid, upload, i, s, name, types[f"t_{table}.c_{column}"])) is not None:
            if column == key[4] and types[f"t_{table}.c_{column}"] == "TEXT":   # as compared with the store's keys:
                value = {"type": "column", "source": f"t_{names[upload]}", "reference": f"c_{name}"}   # not trimmed
            cols.append(f"c_{column}")
            vals.append(value)
    part["insert"] = {"columns": cols, "values": vals, "from": f"t_{names[upload]}", "key": f"c_{key[3]}",
                      "store_key": f"c_{key[4]}"}


async def run(prompt: str, model, db, step=lambda text: None) -> dict:
    """The PoC's run(), with `model` opened by the caller and progress through `step` (no printing). Raises Paused
    after the first step whose warnings carry a question; its notices are those of the steps run so far."""
    store = store_of(db)
    model, warned = _Ordered(model), {}                       # step → its latest warnings (lists changed in place too)

    def pause(*names, **sections):
        warned.update((k, v["warnings"]) for k, v in sections.items())
        for name in names:                                    # one prompt: the first call of the step with a question
            found = [{"step": name} | w for w in warned[name] if "question" in w]
            if questions := first_questions(found, lambda w: w["step"], model.order):
                raise Paused(name, questions, [{"step": k} | w for k, ws in warned.items() for w in ws
                                               if "question" not in w])

    step("source selection")
    selected = await source_route.run(model, prompt, db)
    pause("source_selection", source_selection=selected)
    incoming = selected["incoming"]
    named = ", ".join(t["sql_name"] for t in incoming)

    def stop(problem, stage="source_selection", **sections):
        return {"prompt": prompt, "source_selection": selected, **sections,
                "assembly": {"program": [], "questions": [], "errors": [
                    {"request": "message", "stage": stage, "error": problem}]}}
    if selected.get("error") or not selected["store"]:
        return stop(selected.get("error") or (f"Selected incoming source(s): {named}. Reading an upload without the "
                                              "store is not implemented yet." if named
                                              else "No data source could be identified for this prompt."))
    if incoming:                                              # the store's records updated from an upload
        store, problem = with_incoming(db, store, incoming)
        if problem:
            return stop(problem)

    spans = extractor.connect()                                # GLiNER2 is needed only for the store compiler

    step("references (CorPipe)")
    mentions = coref.mentions([prompt], name="pipeline")[0]
    step("step 1 · requests, links, references")
    s1 = await request_graph.run(model, prompt, mentions, spans, request_graph.store_text(store))
    pause("requests", requests=s1)
    step("step 2 · operation")
    s2 = await operation.run(model, prompt, s1["requests"], store)
    several = [rid for rid, x in s2["requests"].items() if x.get("several_actions")]
    if several:                                               # two actions in one request: step 1 cuts it again
        again = await request_graph.recut(model, prompt, s1, several[0], spans)
        if again:
            s1 = again
            step("step 2 · operation, after cutting a request with two actions")
            s2 = await operation.run(model, prompt, s1["requests"], store)
    pause("requests", "operation", requests=s1, operation=s2)  # after the recut: its cut and links are step 1's
    if incoming and (other := {x["statement"] for x in s2["requests"].values()} - {"update", "insert"}):
        return stop(f"Selected incoming source(s): {named}. Only an update or an insert of the store's records from "
                    f"an upload is built yet, not {', '.join(sorted(other))}.", "operation", requests=s1, operation=s2)
    reqs = [r | {k: v for k, v in s2["requests"][r["id"]].items() if k in ("statement", "input")}
            for r in s1["requests"]]
    step("step 3 · values")
    s3 = await values.run(model, db, prompt, reqs, store, spans)
    pause("values", values=s3)
    for to, frm in s1.get("inherits", {}).items():          # "once you have lowered them": the other part's values
        s3["values"] += [v | {"id": f"{v['id']}_{to}", "request": to, "inherited_from": frm} for v in s3["values"]
                         if v["request"] == frm and v.get("role") == "lookup" and v.get("column")]
    step("step 4 · sources")
    s4 = await sources.run(model, prompt, reqs, store, s1["references"], s3["values"])
    pause("sources", sources=s4)
    step("step 5 · parts")
    s5 = await parts.run(model, db, prompt, reqs, store, s4["requests"], s3["values"], numeric_by_formula=True)
    pause("parts", parts=s5)
    def converted(asked, errors, **sections):                # step 6b, and again after step 9: a stop or a question
        if errors:
            return {"prompt": prompt, "source_selection": selected, "requests": s1, "operation": s2, "parts": s5,
                    **sections, "assembly": {"program": [], "questions": errors, "errors": errors}}
        if asked:                                             # a formula to give: the message's question
            raise Paused("conversion", asked, [{"step": k} | w for k, ws in warned.items() for w in ws
                                               if "question" not in w])

    if incoming:
        asked, errors = from_incoming(db, store, incoming, reqs, s5["requests"], s4["requests"])
        s5["warnings"] = [w for w in s5["warnings"] if not ((s5["requests"].get(w.get("request")) or {}).get(
            "insert", {}).get("from") and w.get("problem", "").startswith("value of "))]   # the upload gives them
        if stopped := converted(asked, errors):
            return stopped
    step("step 6 · conditions")
    s6 = await conditions.run(model, prompt, reqs, store, s4["requests"], s3["values"], s5["requests"], db, spans)
    pause("conditions", conditions=s6)
    step("step 6 · grouping, sort, limit")
    g = await grouping.run(model, prompt, reqs, store, s4["requests"], s6, s6.get("parameters", {}))
    pause("grouping", grouping=g)
    step("step 5 · numeric changes")
    linked = []                                               # (request, a saved link's assignment, step 9's copy)

    def mine(rid, x):                                         # step 9 sees a copy: the link's value stays unless the
        if x.get("from") != "incoming":                       # message names an upload's column for it
            return x
        linked.append((rid, x, copy := {"column": x["column"], "value": x["value"]}))
        return copy
    own = {rid: {**p, "assignments": [mine(rid, x) for x in p.get("assignments", [])]}
           for rid, p in s5["requests"].items()}
    f5 = await formula.run(model, prompt, reqs, store, s4["requests"], s3["values"], own, s6, g, db)
    for rid, x, copy in linked:
        if copy.get("from") == "formula" and reads(copy["value"], incoming):
            x["value"] = copy["value"]                        # read again below, by its own pair's units
            x.pop("from")
        else:                                                 # the link's value: step 9's doubts about it don't apply
            f5["warnings"] = [w for w in f5["warnings"] if not (w.get("request") == rid and w.get(
                "problem", "").startswith(f"formula of {x['column']}"))]
    pause("formula", formula=f5)
    if incoming and (stopped := converted(*from_uploads(db, store, incoming, reqs, s5["requests"]),
                                          conditions=s6, grouping=g, formula=f5)):
        return stopped
    step("step 7 · assembly")
    a = assemble.assemble(db, store, reqs, s2["requests"], s4["requests"], s5["requests"], s6["requests"],
                          g["requests"], [("requests", s1["warnings"]), ("operation", s2["warnings"]),
                                          ("values", s3["warnings"]), ("sources", s4["warnings"]),
                                          ("parts", s5["warnings"]), ("conditions", s6["warnings"]),
                                          ("grouping", g["warnings"]), ("formula", f5["warnings"])],
                          order=s1.get("order"))
    return {"prompt": prompt, "requests": s1, "operation": s2, "values": s3, "sources": s4, "parts": s5,
            "conditions": s6, "grouping": g, "formula": f5, "assembly": a}
