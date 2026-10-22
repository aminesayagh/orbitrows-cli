"""One prompt → a checked SQL program on the store's tables: steps 1 to 7 of the PoC (v2/prompt_ast/pipeline.py), in
its order, each step fed the previous steps' outputs. Paid: the model; local: CorPipe and GLiNER2.

    store   the store's tables read back from SQLite (ddl.schema_from_sqlite), without the CLI's own tables (`_…`)
    out     {prompt, requests, operation, values, sources, parts, conditions, grouping, formula, assembly}; assembly is
            {"program", "questions", "errors"}: step 7 runs the program in a savepoint, rolled back (nothing is changed)

A model answer below a step's threshold is a question for the user (asking.py): run stops after the first step that
has one (Paused, as the store import); once answered (client.settle), it runs again from the start, replaying the
recorded answers (client.Recorded). Every other warning is a notice: shown, and the pipeline goes on.
"""
from orbitrows.pipeline.store.run import Paused, _Ordered, first_questions
from orbitrows.services.extractor import client as extractor
from orbitrows.sql.ddl import schema_from_sqlite

from . import assemble, conditions, coref, formula, grouping, operation, parts, request_graph, sources, values


def store_of(db) -> list[dict]:
    """The store's create_table nodes, as the PoC's Orbitrow schema(); `_` tables and columns are the CLI's
    (classification, history; a file's row order), not the merchant's."""
    nodes = [n for n in schema_from_sqlite(db) if not n.definition.sql_name.startswith("_")]
    for n in nodes:
        n.definition.columns = [c for c in n.definition.columns if not c.sql_name.startswith("_")]
    return [n.model_dump() for n in nodes]


async def run(prompt: str, model, db, step=lambda text: None) -> dict:
    """The PoC's run(), with `model` opened by the caller and progress through `step` (no printing). Raises Paused
    after the first step whose warnings carry a question; its notices are those of the steps run so far."""
    store, spans = store_of(db), extractor.connect()           # spans: GLiNER2, the PoC's `model`
    model, warned = _Ordered(model), {}                       # step → its latest warnings (lists changed in place too)

    def pause(*names, **sections):
        warned.update((k, v["warnings"]) for k, v in sections.items())
        for name in names:                                    # one prompt: the first call of the step with a question
            found = [{"step": name} | w for w in warned[name] if "question" in w]
            if questions := first_questions(found, lambda w: w["step"], model.order):
                raise Paused(name, questions, [{"step": k} | w for k, ws in warned.items() for w in ws
                                               if "question" not in w])

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
    step("step 6 · conditions")
    s6 = await conditions.run(model, prompt, reqs, store, s4["requests"], s3["values"], s5["requests"], db, spans)
    pause("conditions", conditions=s6)
    step("step 6 · grouping, sort, limit")
    g = await grouping.run(model, prompt, reqs, store, s4["requests"], s6, s6.get("parameters", {}))
    pause("grouping", grouping=g)
    step("step 5 · numeric changes")
    f5 = await formula.run(model, prompt, reqs, store, s4["requests"], s3["values"], s5["requests"], s6, g, db)
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
