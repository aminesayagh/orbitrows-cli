"""One prompt → a checked SQL program on the store's tables: steps 1 to 7 of the PoC (v2/prompt_ast/pipeline.py), in
its order, each step fed the previous steps' outputs. Paid: the model; local: CorPipe and GLiNER2.

    store   the store's tables read back from SQLite (ddl.schema_from_sqlite), without the CLI's own tables (`_…`);
            with the selected uploads' tables, keyed and linked in that view only (with_incoming): an update takes,
            through the key, the upload column a step names (its type the one its cells show: cell_type; read and
            converted by the user's formula: from_uploads),
            else the one that fits it (resolve: by type and unit, the model choosing among several); an insert takes
            the upload's records whose key the store doesn't have (insert_from), each column resolved the same way;
            a read runs on the uploads as on the store's tables, a write whose target is an upload is refused (read_only)
    out     {prompt, requests, operation, values, sources, parts, conditions, grouping, formula, assembly};
            assembly is {"program", "questions", "errors"}: step 7 runs the program in a savepoint, rolled back
            (nothing is changed); a value source is resolved by each message, never saved

A model answer below a step's threshold is a question for the user (asking.py): run stops after the first step that
has one (Paused, as the store import); once answered (client.settle), it runs again from the start, replaying the
recorded answers (client.Recorded). Every other warning is a notice: shown, and the pipeline goes on.
"""
import json

from orbitrows.pipeline.merge import convert
from orbitrows.pipeline.store.profile import facts
from orbitrows.pipeline.store.run import Paused, _Ordered, first_questions
from orbitrows.services.extractor import client as extractor
from orbitrows.services.storage.store import KINDS, ROLES
from orbitrows.sql.ddl import schema_from_sqlite

from . import assemble, conditions, coref, formula, grouping, guard, operation, parts, request_graph, source_route, sources, values



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


def with_incoming(db, store: list[dict], incoming: list[dict]) -> list[dict]:
    """The store's nodes and each selected upload's, linked in this view only (never in the database): the upload's
    key is its column referencing a store key (its table's primary key or a dedicated_key column; keyed). A key on
    one row each in the upload's profile is followed both ways (one-to-one: the store record reaches
    the upload's row and the row the store record, each along a many-to-one "up" key); a key that repeats (one row per
    sku and warehouse) only from the upload to the store record (many-to-one: its rows grouped or totalled per record;
    a write from it is refused, repeated); an upload without a key link is read alone. A non-key column is typed by
    its cells (cell_type), read as a number where it holds numbers (assemble.typed)."""
    by_name = {n.definition.sql_name: n.model_dump() for n in schema_from_sqlite(db)}
    tables = {n["definition"]["sql_name"]: n["definition"] for n in store}
    out = list(store)
    for t in incoming:
        links = keyed(db, t["id"], tables)
        mine = by_name[t["sql_name"]]["definition"]
        for c in mine["columns"]:                             # a key keeps its type: as compared with the store's
            if c["sql_name"] not in {k for k, *_ in links}:
                c["sql_type"] = cell_type(db, t["sql_name"], c["sql_name"])
        for key, table, column in links:
            if unique(db, t["id"], t["sql_name"], key):       # one-to-one: the store record reaches the row too
                mine["primary_key"] = mine.get("primary_key") or [f"c_{key}"]
                tables[table]["foreign_keys"] = tables[table].get("foreign_keys", []) + [
                    {"columns": [f"c_{column}"], "references": {"table": mine["table_id"], "columns": [f"c_{key}"]},
                     "virtual": True}]                         # this view's, not the database's: nothing guaranteed
            mine["foreign_keys"] = mine.get("foreign_keys", []) + [
                {"columns": [f"c_{key}"], "references": {"table": tables[table]["table_id"],
                                                         "columns": [f"c_{column}"]}, "virtual": True}]
        out.append(by_name[t["sql_name"]])
    return out


def read_only(incoming: list[dict], reqs: list[dict], sources_: dict) -> str | None:
    """Why the message can't run: a write (update, insert, delete, alter, create, drop) whose target is an upload, a
    read-only copy of its file (the database refuses it too: triggers), or None. Every read runs."""
    files = {f"t_{t['sql_name']}": t for t in incoming}
    for r in reqs:
        if r["statement"] != "select" and (t := files.get(parts.main_table(reqs, sources_, r["id"]))):
            return f"{t['sql_name']} is a read-only copy of {t['source_file']}: it can't be changed."
    return None


def cell_type(db, table: str, column: str) -> str:
    """An upload column's type as its cells show it (every cell is TEXT in the database: a raw snapshot): INTEGER
    when each non-empty cell is a whole number, REAL when each is a plain number, else TEXT (an empty column too).
    The view's only: a step may then name it for a number change; its value is cast when read (from_uploads). An
    identifier written with digits (a barcode, a reference: the roles step's kind) stays TEXT: a code, not a number."""
    kind = db.execute("SELECT ic.kind FROM _incoming_columns ic JOIN _incoming_tables t ON t.id = ic.table_id "
                      "WHERE t.sql_name = ? AND ic.sql_name = ?", (table, column)).fetchone()
    if kind and kind[0] is not None and KINDS[kind[0]] in ("record_reference", "model_reference"):
        return "TEXT"
    return facts(db, table, column).get("cells", "TEXT")      # counted at upload: the rows are never read


def keyed(db, upload: int, tables: dict) -> list[tuple]:
    """The upload's key links [(its key column, store table, store column)]: a column whose references_column_id
    (set at upload: incoming_match.link_keys) is a store key (its table's primary key or a dedicated_key column).
    The update (with_incoming) and the insert (insert_from) read the same ones."""
    links = db.execute(
        "SELECT ic.sql_name, st.sql_name, sc.sql_name, sc.role FROM _incoming_columns ic "
        "JOIN _store_columns sc ON sc.id = ic.references_column_id JOIN _store_tables st ON st.id = sc.table_id "
        "WHERE ic.table_id = ? ORDER BY ic.position", (upload,)).fetchall()
    return [(key, table, column) for key, table, column, role in links if table in tables and (
        role == ROLES.index("dedicated_key") or tables[table]["primary_key"] == [f"c_{column}"])]


def unique(db, upload: int, table: str, key: str) -> bool:
    """Is the upload's key on one row each (one-to-one)? As its profile counted it (max_repeat)."""
    return not repeated(db, table, key)


def repeated(db, table: str, key: str) -> str | None:
    """Why the upload can't be matched on `key`: a key value on several rows (the store's tables don't enforce
    their keys, so an insert would add the record twice and an update would take one row of them), or None."""
    found = facts(db, table, key).get("repeats", [])          # the profile's: the rows are never read
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
        if upload not in checked:                             # one key on a row each is enough to match the rows
            why = [repeated(db, names[upload], k) for k in dict.fromkeys(k for k, *_ in keys[upload])]
            checked[upload] = None if not all(why) else why[0]  # a repeating reference (a product per variant) too
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
            bad = facts(db, names[upload], name).get("not_whole" if whole else "not_number", [])   # its profile
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


def candidates(db, upload: int, s: int, typ: str) -> list[tuple]:
    """The upload's columns that may supply store column s (typ its type), by code: not a key, not a context column;
    number cells for a number column (cell_type); the same kind of unit when both have one (a currency for a
    currency: the units themselves are converted, convert.py). → [(incoming column id, sql name, header)], in file
    order."""
    (kind,) = db.execute("SELECT cc.unit_kind FROM _store_columns sc LEFT JOIN _column_context cc "
                         "ON cc.id = sc.unit_id WHERE sc.id = ?", (s,)).fetchone()
    (table,) = db.execute("SELECT sql_name FROM _incoming_tables WHERE id = ?", (upload,)).fetchone()
    found = db.execute(
        "SELECT ic.id, ic.sql_name, ic.source_header, cc.unit_kind FROM _incoming_columns ic "
        "LEFT JOIN _column_context cc ON cc.id = ic.unit_id WHERE ic.table_id = ? "
        "AND NOT COALESCE(json_extract(ic.extra_json, '$.primary_key_candidate'), 0) "
        "AND ic.references_column_id IS NULL AND json_extract(ic.extra_json, '$.context') IS NULL "
        "ORDER BY ic.position", (upload,)).fetchall()
    return [(i, name, header) for i, name, header, k in found
            if (typ not in ("INTEGER", "REAL") or cell_type(db, table, name) != "TEXT")
            and (kind is None or k is None or k == kind)]


async def choose(model, db, prompt: str, wanted: list[tuple], warnings: list) -> dict:
    """One model call for every store column a message needs a source for among several candidates (wanted:
    [(key, store column s, upload, [candidates()])]): the matching question's criteria and rules (incoming_match),
    as one choice among the candidates or none. → {key: (incoming column id, sql name), or None (none: no source)};
    a key below SURE is left out, its question in `warnings` (step "matching")."""
    from orbitrows.pipeline import incoming_match
    from orbitrows.services.model.client import SURE, ask, choice, options
    questions, upload, labels = {}, None, {}
    for key, s, upload, found in wanted:
        table, column, header, typ = db.execute(
            "SELECT st.sql_name, sc.sql_name, sc.source_header, sc.sql_type FROM _store_columns sc "
            "JOIN _store_tables st ON st.id = sc.table_id WHERE sc.id = ?", (s,)).fetchone()
        labels[key] = f"{table}.{column}"
        questions[key] = choice(
            "Map an incoming CSV to an existing store catalog.",
            "Which incoming column supplies this store column for the message?",
            {name: f"{h}: {incoming_match.CRITERIA['direct']}" for _, name, h in found}
            | {"none": incoming_match.CRITERIA["none"]}, incoming_match.RULES[1:],
            message=prompt, store_column={"table": table, "column": column, "header": header, "type": typ})
    if not questions:
        return {}
    (name,) = db.execute("SELECT sql_name FROM _incoming_tables WHERE id = ?", (upload,)).fetchone()
    columns = sorted({c for *_, found in wanted for c in found})
    state = {"incoming": {"table": name, "columns": [{"name": c[1], "header": c[2],         # the profile's samples
                                                      "samples": facts(db, name, c[1]).get("samples", [])}
                                                     for c in columns]}}
    answers = await ask(model, state, questions)
    out = {}
    for key, s, upload, found in wanted:
        selected, probabilities = answers[key]
        if probabilities.get(selected, 0) >= SURE:
            out[key] = next(((i, n) for i, n, _ in found if n == selected), None)
            continue
        warnings.append({"request": key.rsplit("_s", 1)[0],
                         "problem": f"Which column of {name} supplies {labels[key]}?",
                         "probability": round(probabilities.get(selected, 0), 2),
                         "question": {"type": "choice", "call": answers.call, "key": key,
                                      "options": options(probabilities)}})
    return out


async def from_incoming(model, prompt, db, store: list[dict], incoming: list[dict], reqs: list[dict], parts_: dict,
                        sources_: dict) -> tuple[list, list, list]:
    """An insert takes the upload's new records (insert_from), each value converted (taken). → (questions: a formula
    to give, errors: a unit varying row by row, a key repeated, a number cell that isn't a plain number, matching:
    an insert's uncertain source column). An update's changed column is resolved by the message: the upload's column
    the formula stage names, else after it (resolve); no pair is saved for the next one."""
    asked, errors, matching = [], [], []
    names = {t["id"]: t["sql_name"] for t in incoming}
    taken = reader(db, store, incoming, asked, errors)
    types = {f"{n['definition']['table_id']}.{c['column_id']}": c["sql_type"] for n in store
             for c in n["definition"]["columns"]}
    tables = {n["definition"]["sql_name"]: n["definition"] for n in store}
    keys = {t["id"]: keyed(db, t["id"], tables) for t in incoming}
    for r in reqs:
        if r["statement"] == "insert":
            await insert_from(model, prompt, db, r["id"], parts.main_table(reqs, sources_, r["id"]), names,
                              types, taken, parts_.setdefault(r["id"], {}), errors, keys, matching)
    return asked, errors, matching


async def resolve(model, prompt, db, store: list[dict], incoming: list[dict], open_: list, warnings: list) -> list:
    """An update's assignments (open_: [(request, assignment)]) still without a value after the formula (no column named
    by the message): the upload's column that supplies it, by code (candidates: 0 a stop, 1 taken),
    else the model's choice among them (choose), uncertain a question. The assignment then reads the chosen column:
    from_uploads converts it; the next message resolves its own. → errors."""
    tables = {n["definition"]["sql_name"]: n["definition"] for n in store}
    names = {t["id"]: t["sql_name"] for t in incoming}
    errors, wanted, chosen, todo = [], [], {}, []
    for rid, a in open_:
        table, column = (x.split("_", 1)[1] for x in a["column"].split("."))
        upload = next((t["id"] for t in incoming if any(x[1] == table for x in keyed(db, t["id"], tables))),
                      incoming[0]["id"])
        (s,) = db.execute("SELECT sc.id FROM _store_columns sc JOIN _store_tables st ON st.id = sc.table_id "
                          "WHERE st.sql_name = ? AND sc.sql_name = ?", (table, column)).fetchone()
        typ = next(c["sql_type"] for c in tables[table]["columns"] if c["sql_name"] == column)
        found, key = candidates(db, upload, s, typ), f"{rid}_s{s}"
        if not found:
            errors.append({"request": rid, "stage": "matching", "error": (
                f"no column of {names[upload]} can supply {table}.{column} (none holds "
                f"{'numbers' if typ in ('INTEGER', 'REAL') else 'values'} of its kind) and the message names none: "
                "nothing is guessed. Name the column to take it from.")})
            continue
        if len(found) == 1:
            chosen[key] = found[0][:2]
        else:
            wanted.append((key, s, upload, found))
        todo.append((rid, a, key, upload, s, f"{table}.{column}"))
    chosen |= await choose(model, db, prompt, wanted, warnings)
    for rid, a, key, upload, s, label in todo:
        if key not in chosen:
            continue
        if chosen[key] is None:
            errors.append({"request": rid, "stage": "matching", "error": f"no column of {names[upload]} supplies "
                           f"{label}: nothing is guessed. Name the column to take it from."})
            continue
        a.update({"value": {"type": "column", "source": f"t_{names[upload]}", "reference": f"c_{chosen[key][1]}"},
                  "from": "formula"})
        a.pop("pending", None)
    return errors


def reads(value, incoming: list[dict]) -> bool:
    """Does the value read a column of a selected upload?"""
    if isinstance(value, dict):
        return value.get("type") == "column" and value.get("source") in {f"t_{t['sql_name']}" for t in incoming} \
            or any(reads(v, incoming) for v in value.values())
    return isinstance(value, list) and any(reads(v, incoming) for v in value)


def from_uploads(db, store: list[dict], incoming: list[dict], reqs: list[dict], parts_: dict) -> tuple[list, list]:
    """An update's value that reads an upload's column, whichever step chose it (step 5's parts, the formula stage's:
    "set the price to the recommended price of supplier_offer.csv", resolve), each cell read as a link's is (reader):
    trimmed, an empty one NULL and no write (assembly, from: incoming), cast for a number column, converted by the
    units of that (upload column, store column) pair; a value already read (from: incoming) is not read twice.
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


async def insert_from(model, prompt, db, rid, main, names, types, taken, part, errors, keys, matching):
    """An insert into `main` from the first selected upload whose key is linked to it (keys: keyed()'s links, as
    with_incoming chose them): its key, and each other column of `main` (only those step 5 says the request gives,
    when it names some) from the upload's column that supplies it, resolved by this message (candidates; several:
    choose, an uncertain choice a question in `matching`; a column chosen for one store column isn't offered for
    another), each value converted (taken); assembly reads the upload's rows whose key is not empty and not in the
    store yet. Never guessed: a NOT NULL store column without default and without a source, or a key linked to a
    parent table (the record would need one there), stops the message."""
    table = main and main.split("_", 1)[1]
    upload = next((u for u in names if any(t == table for _, t, _ in keys[u])), None)
    if upload is None:
        errors.append({"request": rid, "stage": "insert", "error": f"no selected upload has a key linked to {table}: "
                                                                   "its new records can't be told; nothing is guessed."})
        return
    parents = {p for (_, _, p, *_) in db.execute(f'PRAGMA foreign_key_list("{table}")')}
    if split := sorted({f"{t}.{c}" for _, t, c in keys[upload] if t in parents}):
        errors.append({"request": rid, "stage": "insert", "error": f"{', '.join(split)} live in another table: a new "
                       f"{table} record would need its record there first, which is not built yet."})
        return
    gives = set((part.get("insert") or {}).get("gives") or [])
    name, _, column = next(k for k in keys[upload] if k[1] == table)
    key = db.execute("SELECT ic.id, sc.id FROM _incoming_columns ic JOIN _store_columns sc "
                     "ON sc.id = ic.references_column_id WHERE ic.table_id = ? AND ic.sql_name = ?",
                     (upload, name)).fetchone()
    key = (upload, key[0], key[1], name, column, table)
    mine = {column: key}                                      # store column → its source, as key
    info = db.execute(f'PRAGMA table_info("{table}")').fetchall()
    ids = dict(db.execute("SELECT sc.sql_name, sc.id FROM _store_columns sc JOIN _store_tables st "
                          "ON st.id = sc.table_id WHERE st.sql_name = ?", (table,)))
    found = {n: candidates(db, upload, ids[n], typ.upper()) for _, n, typ, nn, default, pk in info
             if n not in mine and n in ids and not (pk and typ.upper() == "INTEGER")
             and not (gives and f"t_{table}.c_{n}" not in gives)}   # the table's other columns: resolved here
    used, one = set(), {}                                     # a column with one source left takes it, which is
    while single := next((n for n, f in found.items() if len([c for c in f if c[0] not in used]) == 1), None):
        (chosen,) = [c for c in found.pop(single) if c[0] not in used]   # then no other column's (in this insert)
        one[f"{rid}_s{ids[single]}"] = chosen[:2]
        used.add(chosen[0])
    wanted = [(f"{rid}_s{ids[n]}", ids[n], upload, left) for n, f in found.items()
              if (left := [c for c in f if c[0] not in used])]
    asked = len(matching)
    for k, chosen in (one | await choose(model, db, prompt, wanted, matching)).items():
        if chosen:
            column = next(n for n, s in ids.items() if f"{rid}_s{s}" == k)
            mine[column] = (upload, chosen[0], ids[column], chosen[1], column, table)
    if len(matching) > asked:                                 # a source to choose: asked first
        return
    required = [n for _, n, typ, nn, default, pk in info
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

    step("guardrail")
    if routed := await guard.run(model, prompt):              # not a database request: nothing runs
        return {"prompt": prompt, "assembly": {"program": [], "questions": [], "errors": [
            {"request": "message", "stage": "guardrail", "error": routed}]}}
    step("source selection")
    selected = await source_route.run(model, prompt, db)
    pause("source_selection", source_selection=selected)
    incoming = selected["incoming"]

    def stop(problem, stage="source_selection", **sections):
        return {"prompt": prompt, "source_selection": selected, **sections,
                "assembly": {"program": [], "questions": [], "errors": [
                    {"request": "message", "stage": stage, "error": problem}]}}
    if selected.get("error") or not (selected["store"] or incoming):
        return stop(selected.get("error") or "No data source could be identified for this prompt.")
    if incoming:                                              # one database: the store's tables and the uploads'
        store = with_incoming(db, store, incoming)

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
    if apps := [r["text"] for r in s1["requests"] if s2["requests"][r["id"]].get("statement") == "application"]:
        return stop(f"Not a database request: '{apps[0]}' is an application action (a backup, an undo, a question "
                    f"about the tool), so nothing of the message was run; run its command (`orbitrows --help`), then "
                    f"send the data part as its own message", "operation", requests=s1, operation=s2)
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
    if problem := read_only(incoming, reqs, s4["requests"]):  # the one rule for an upload: never written
        return stop(problem, "sources", requests=s1, operation=s2, sources=s4)
    step("step 5 · parts")
    s5 = await parts.run(model, db, prompt, reqs, store, s4["requests"], s3["values"])
    pause("parts", parts=s5)
    def converted(asked, errors, **sections):                # step 6b, and after the formula: a stop or a question
        if errors:
            return {"prompt": prompt, "source_selection": selected, "requests": s1, "operation": s2, "parts": s5,
                    **sections, "assembly": {"program": [], "questions": errors, "errors": errors}}
        if asked:                                             # a formula to give: the message's question
            raise Paused("conversion", asked, [{"step": k} | w for k, ws in warned.items() for w in ws
                                               if "question" not in w])

    if incoming:
        asked, errors, matching = await from_incoming(model, prompt, db, store, incoming, reqs, s5["requests"],
                                                      s4["requests"])
        s5["warnings"] = [w for w in s5["warnings"] if not ((s5["requests"].get(w.get("request")) or {}).get(
            "insert", {}).get("from") and w.get("problem", "").startswith("value of "))]   # the upload gives them
        pause("matching", matching={"warnings": matching})
        if stopped := converted(asked, errors):
            return stopped
    step("step 6 · stages 1–2 · conditions")
    s6 = await conditions.run(model, prompt, reqs, store, s4["requests"], s3["values"], s5["requests"], db, spans)
    pause("conditions", conditions=s6)
    step("step 6 · stage 3 · numeric changes")               # before grouping (the PoC ran it after): its branches
    unlinked = []                                             # (request, a pending assignment of an update from an
    updates = {r["id"] for r in reqs if r["statement"] == "update"}   # upload, the formula's copy)

    def mine(rid, x):                                         # the formula sees a copy: a pending one is resolved after
        if incoming and x.get("pending") and rid in updates:  # it, unless the message's words give its value
            unlinked.append((rid, x, copy := dict(x)))
            return copy
        return x
    own = {rid: {**p, "assignments": [mine(rid, x) for x in p.get("assignments", [])]}
           for rid, p in s5["requests"].items()}
    f5 = await formula.run(model, prompt, reqs, store, s4["requests"], s3["values"], own, s6, {"requests": {}}, db)
    open_ = []                                                # no value from the message's words: resolved below
    for rid, x, copy in unlinked:
        about = lambda w: w.get("request") == rid and x["column"] in w.get("problem", "")
        if copy.get("from") == "formula":
            x.update({"value": copy["value"], "from": "formula"})
            x.pop("pending", None)
        elif not any("question" in w for w in f5["warnings"] if about(w)):   # the formula: nothing to ask, no words
            f5["warnings"] = [w for w in f5["warnings"] if not about(w)]
            open_.append((rid, x))
    pause("formula", formula=f5)
    step("step 6 · stage 4 · grouping")      # the WHERE's tree, without the CASE's branches: a
    tree_of = json.loads(json.dumps(s6))                       # branch ("but for the black ones") chooses no rows
    for rid, done in f5.get("branches", {}).items():
        c = tree_of["requests"].get(rid) or {}
        c["predicates"] = [x for x in c.get("predicates", []) if x["condition"] not in done]
    for rid, part in s5["requests"].items():                  # step 5 found an order by a value it computes: the
        if isinstance(part.get("outputs"), list) and any(o.get("sort") for o in part["outputs"]):   # sort question
            tree_of["requests"].setdefault(rid, {})["computed_sort"] = True                      # offers it
    g = await grouping.run(model, prompt, reqs, store, s4["requests"], tree_of, s6.get("parameters", {}))
    pause("grouping", grouping=g)
    if open_:
        matching = []
        if errors := await resolve(model, prompt, db, store, incoming, open_, matching):
            return converted([], errors, conditions=s6, grouping=g, formula=f5)
        pause("matching", matching={"warnings": matching})
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
