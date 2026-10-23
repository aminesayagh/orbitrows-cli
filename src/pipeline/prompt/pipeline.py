"""One prompt → a checked SQL program on the store's tables: steps 1 to 7 of the PoC (v2/prompt_ast/pipeline.py), in
its order, each step fed the previous steps' outputs. Paid: the model; local: CorPipe and GLiNER2.

    store   the store's tables read back from SQLite (ddl.schema_from_sqlite), without the CLI's own tables (`_…`);
            with the selected uploads' tables, keyed and linked in that view only (with_incoming): an update takes
            a linked upload column's value through the key, converted by the user's formula (from_incoming)
    out     {prompt, requests, operation, values, sources, parts, conditions, grouping, formula, assembly}; assembly is
            {"program", "questions", "errors"}: step 7 runs the program in a savepoint, rolled back (nothing is changed)

A model answer below a step's threshold is a question for the user (asking.py): run stops after the first step that
has one (Paused, as the store import); once answered (client.settle), it runs again from the start, replaying the
recorded answers (client.Recorded). Every other warning is a notice: shown, and the pipeline goes on.
"""
import json

from orbitrows.pipeline.merge import convert
from orbitrows.pipeline.store.run import Paused, _Ordered, first_questions
from orbitrows.services.extractor import client as extractor
from orbitrows.services.storage.store import ROLES
from orbitrows.sql.ddl import schema_from_sqlite

from . import assemble, conditions, coref, formula, grouping, operation, parts, request_graph, source_route, sources, values


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
    matched = db.execute("SELECT 1 FROM sqlite_schema WHERE name = '_incoming_store_matches'").fetchone()
    out = list(store)
    for t in incoming:
        links = matched and db.execute(
            "SELECT ic.sql_name, st.sql_name, sc.sql_name, sc.role FROM _incoming_columns ic "
            "JOIN _incoming_store_matches m ON m.incoming_column_id = ic.id AND m.relation = 'direct' "
            "JOIN _store_columns sc ON sc.id = m.store_column_id JOIN _store_tables st ON st.id = sc.table_id "
            "WHERE ic.table_id = ? AND json_extract(ic.extra_json, '$.primary_key_candidate')", (t["id"],)).fetchall()
        links = [(key, table, column) for key, table, column, role in links or [] if table in tables and (
            role == ROLES.index("dedicated_key") or tables[table]["primary_key"] == [f"c_{column}"])]
        if not links:
            return store, (f"{t['sql_name']} ({t['source_file']}) has no key column linked to a store key (a table's "
                           "primary key or reference code), so its rows can't be matched to the store's records; "
                           "nothing is guessed. Reading an upload without such a link is not implemented yet.")
        node = by_name[t["sql_name"]]
        node["definition"]["primary_key"] = [f"c_{links[0][0]}"]
        for key, table, column in links:
            tables[table]["foreign_keys"] = tables[table].get("foreign_keys", []) + [
                {"columns": [f"c_{column}"], "references": {"table": node["definition"]["table_id"],
                                                            "columns": [f"c_{key}"]}}]
        out.append(node)
    return out, None


def from_incoming(db, store: list[dict], incoming: list[dict], reqs: list[dict], parts_: dict) -> tuple[list, list]:
    """An update's changed column directly matched to a (non-key) column of a selected upload takes that column's
    value, which assembly reaches through the key: as is (cast to REAL for a number column: incoming cells are TEXT),
    or converted by the user's formula (convert.py). → (questions: a formula to give, errors: a unit varying row by
    row); each assignment so built is set (from: incoming), no longer pending."""
    asked, errors = [], []
    plans = {t["id"]: convert.plan(db, t["id"]) for t in incoming}
    names = {t["id"]: t["sql_name"] for t in incoming}
    types = {f"{n['definition']['table_id']}.{c['column_id']}": c["sql_type"] for n in store
             for c in n["definition"]["columns"]}
    for r in reqs:
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
            plan, typ = plans[upload], types[a["column"]]
            value = {"type": "column", "source": f"t_{names[upload]}", "reference": f"c_{name}"}
            if typ in ("INTEGER", "REAL"):
                value = {"type": "function", "name": "real", "arguments": [value]}
            if refused := next((x for x in plan["refused"] if x["pair"] == (i, s)), None):
                errors.append({"request": r["id"], "stage": "conversion", "error": refused["reason"]})
                continue
            if need := next((x for x in plan["needs"] if x["pair"] == (i, s)), None):
                q = convert.question(db, need)
                asked.append({"request": r["id"]} | q | {"question": q["question"] | {
                    "save": [s, need["from"], need["to"]]}})     # the session keeps the answer (convert.save)
                continue
            if formula := next((x["formula"] for x in plan["known"] if x["pair"] == (i, s)), None):
                value = json.loads(json.dumps(convert.parse(formula).model_dump(by_alias=True))
                                   .replace(json.dumps(convert.X.model_dump()), json.dumps(value)))
                if typ == "INTEGER":
                    value = {"type": "function", "name": "round", "arguments": [value]}
            a.update({"value": value, "from": "incoming"})
            a.pop("pending", None)
    return asked, errors


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
    if incoming and (other := {x["statement"] for x in s2["requests"].values()} - {"update"}):
        return stop(f"Selected incoming source(s): {named}. Only an update of the store's records from an upload is "
                    f"built yet, not {', '.join(sorted(other))}.", "operation", requests=s1, operation=s2)
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
    if incoming:
        asked, errors = from_incoming(db, store, incoming, reqs, s5["requests"])
        if errors:
            return {"prompt": prompt, "source_selection": selected, "requests": s1, "operation": s2, "parts": s5,
                    "assembly": {"program": [], "questions": errors, "errors": errors}}
        if asked:                                             # a formula to give: the message's question
            raise Paused("conversion", asked, [{"step": k} | w for k, ws in warned.items() for w in ws
                                               if "question" not in w])
    step("step 6 · conditions")
    s6 = await conditions.run(model, prompt, reqs, store, s4["requests"], s3["values"], s5["requests"], db, spans)
    pause("conditions", conditions=s6)
    step("step 6 · grouping, sort, limit")
    g = await grouping.run(model, prompt, reqs, store, s4["requests"], s6, s6.get("parameters", {}))
    pause("grouping", grouping=g)
    step("step 5 · numeric changes")
    own = {rid: {**p, "assignments": [x for x in p.get("assignments", []) if x.get("from") != "incoming"]}
           for rid, p in s5["requests"].items()}            # a value from an upload is no formula step's
    f5 = await formula.run(model, prompt, reqs, store, s4["requests"], s3["values"], own, s6, g, db)
    pause("formula", formula=f5)
    step("step 7 · assembly")
    a = assemble.assemble(db, store, reqs, s2["requests"], s4["requests"], s5["requests"], s6["requests"],
                          g["requests"], [("requests", s1["warnings"]), ("operation", s2["warnings"]),
                                          ("values", s3["warnings"]), ("sources", s4["warnings"]),
                                          ("parts", s5["warnings"]), ("conditions", s6["warnings"]),
                                          ("grouping", g["warnings"]), ("formula", f5["warnings"])],
                          order=s1.get("order"))
    return {"prompt": prompt, "requests": s1, "operation": s2, "values": s3, "sources": s4, "parts": s5,
            "conditions": s6, "grouping": g, "formula": f5, "assembly": a}
