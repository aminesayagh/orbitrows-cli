"""Step 4 of prompt → AST: the table each request works on (plan S1, vault poc_v2/prompt-to-ast-classification-plan.md).

    Jev   one call per prompt; per request, by statement (step 2):
            select                      from: a store table, or the rows an earlier select returned
            update, delete, alter, drop target: a store table, or a table an earlier request creates
            insert                      target, and where its rows come from: values written in the prompt, a table,
                                        or an earlier select's rows
            create_table                filled from: nothing (empty), a table, or an earlier select's rows
          and, for select / update / delete / insert, per store table: does the request use information stored in it
          (to choose its rows or compute its values)? Those tables, besides the main one, are its `reads` (step 4 turns
          them into a subquery or an IN); a table only linking two others is found from the foreign keys, not asked.
          A read of an earlier select's rows drops that select's table from its reads: its rows carry it.
          step 3's values: a value of the request the store holds in another table makes that table a read, by code
          (SQLite found it there; Jev also sees it). A value step 3 flagged (found elsewhere than the prompt says)
          still counts, with a warning: the user answers step 3's question once, and these reads follow it.
          a read taking an earlier select's rows (step 2: request_result): does it read those same records ("of
          those, keep…": from = that request's rows) or other records about them ("the stock of those": a table)?
    code  "earlier" = a request this one waits for, directly or not (a table or a result exists only then); below 0.8
          the likeliest answer is kept and flagged; "unclear" → a warning, never a guessed table

Section "sources" of cases/<id>.json: {"requests": {"r1": {"target" | "from", "query_from"?, "reads", "flagged"?}},
"warnings", "jev"}. Nodes are the AST's: {"type": "database_table", "reference": "t_…"}, {"type": "request_result",
"request_id"}; {"type": "new_table", "request_id"} stands for the table that request creates (its id is set when its
definition is built). Table ids stay the same after a rename. Step 1's references ("those" → "the variants under 100"
in r1) and step 3's stored values ("Casa Living" → suppliers.name) are shown to Jev with each request. Examples in the rules must not be test prompts (cases/).
"""
from . import SURE
from .asking import question
from orbitrows.services.model.client import ask, choice, yes_no

CONTEXT = ("A user wrote a prompt to edit or read their store database (its tables and columns are in `tables`). "
           "The prompt was split into the requests in `requests`, each with the kind of statement it needs; "
           "`waits_for` names the request that runs first. Each request now needs the table it works on.")
RULES = [
    "The user may name a table in their own words ('the catalog', 'the stock'): choose by meaning, using the "
    "columns.",
    "The table a request changes is the one holding the column it changes: 'raise the price of the Kora lamp' "
    "changes a price, so the table with the price column, even if the lamp's name is stored in another table.",
    "When the user names the table itself ('put a size column in the shelf table'), choose the table that word "
    "means, even if another table already has a similar column.",
    "A renamed table keeps its place: after 'rename the brands table to labels', 'labels' means the brands table.",
    "`refers_to`: words of the request that point back to something written in another request ('those' → 'the "
    "lamps under 50' in r1): the request is about that thing.",
    "`stored_in`: where the store holds a value the request names ('Kora' → brands.name): the request uses that "
    "table to choose its rows, it does not change it.",
    "Answer for this request only.",
    "The records a request shows or changes are the ones it is about, even when a condition counts, totals or "
    "compares their linked records: 'the products whose variants average over 200', 'the suppliers with more than 3 "
    "products', 'the variants whose total stock is under 10' are about the products, the suppliers, the variants; "
    "the variants, the products, the stock lines are only read.",
]
USE_RULES = [
    "Answer yes when the request changes or reads this table, or names one of its values or columns to choose its "
    "rows or to compute a value ('the products of the Kora brand': yes for the table holding brand names).",
    "Answer no for a table the request does not name in any way, even if it links two tables the request uses.",
]
MAIN = {"select": "from", "update": "target", "delete": "target", "insert": "target", "alter_table": "target",
        "drop_table": "target"}
USES = ("select", "update", "delete", "insert")
SAME_RULES = [
    "Answer yes when the request keeps, filters, sorts, counts or shows those same records ('of those, keep the ones "
    "made in Spain', 'sort them by date'), even when its condition names a value from another table.",
    "Answer no when the request is about other records linked to them ('the reviews of those', 'their suppliers').",
]


def ancestors(requests: list[dict], rid: str) -> list[str]:
    """The requests this one waits for, directly or not."""
    by_id, out = {r["id"]: r for r in requests}, []
    w = by_id[rid].get("waits_for")
    while w and w not in out:
        out.append(w)
        w = by_id[w].get("waits_for")
    return out


def options(requests: list[dict], r: dict, schema: list[dict], kind: str) -> dict:
    """kind: main (the table a request changes or reads), rows (an insert's rows), fill (a new table's rows)."""
    copied = "Copied from existing rows of the" if kind in ("rows", "fill") else "The"
    opts = {t["definition"]["table_id"]: f"{copied} {t['definition']['sql_name']} table (columns: "
                                         f"{', '.join(c['sql_name'] for c in t['definition']['columns'])})."
            for t in schema}
    by_id = {x["id"]: x for x in requests}
    reads_rows = kind in ("rows", "fill") or (r["statement"] == "select" and not same_rows(r))
    for a in ancestors(requests, r["id"]):
        if by_id[a]["statement"] == "create_table":
            opts[f"{a}_new_table"] = f"{copied} new table request {a} creates ('{by_id[a]['text']}')."
        if by_id[a]["statement"] == "select" and reads_rows:
            opts[f"{a}_result"] = (f"{'Copied from the' if kind != 'main' else 'The'} rows request {a} returns "
                                   f"('{by_id[a]['text']}'), not a stored table.")
    if kind == "rows":
        opts = {"values": "Written in the prompt: the user gives the new rows' values (names, numbers), even when "
                          "one of them must be looked up in another table.", **opts}
    elif kind == "fill":
        opts["empty"] = "The new table starts empty: only its name or columns are described."
    else:
        opts["unclear"] = "None of these: the prompt does not say which table."
    return opts


def same_rows(r: dict) -> bool:
    """A read taking an earlier request's rows: the rows a select returned (request_result) or the rows an insert,
    update or delete changed (affected_rows, "show the names of those, but first lower …"). Whether it reads those
    rows themselves is its own question (not a table chosen from its words: "names" are in products, "those" are
    variants)."""
    return r["statement"] == "select" and r.get("input") in ("request_result", "affected_rows") and bool(r.get("waits_for"))


def stored(values: list[dict]) -> list[dict]:
    """Step 3's lookups with a column: given by the user, or found by SQLite."""
    return [v for v in values if v.get("role") == "lookup" and v.get("column") and v.get("match") != "none"]


def state(prompt: str, requests: list[dict], schema: list[dict], references: list[dict] = (),
          values: list[dict] = ()) -> dict:
    """references: step 1's (coref.by_request), each {"text", "target", "request", "target_request"}.
    values: step 3's section["values"]."""
    reqs = {r["id"]: {k: r[k] for k in ("text", "waits_for", "statement", "input") if r.get(k) and r.get(k) != "snapshot"}
            for r in requests}
    for ref in references:
        if ref.get("request") in reqs and ref.get("target_request"):
            reqs[ref["request"]].setdefault("refers_to", []).append(
                {"words": ref["text"], "mean": ref["target"], "written_in": ref["target_request"]})
    names = {f"{t['definition']['table_id']}.{c['column_id']}": f"{t['definition']['sql_name']}.{c['sql_name']}"
             for t in schema for c in t["definition"]["columns"]}
    for v in stored(values):
        if v["request"] in reqs:
            reqs[v["request"]].setdefault("stored_in", []).append({"words": v["text"], "column": names[v["column"]]})
    return {"prompt": prompt, "requests": reqs,
            "tables": {t["definition"]["sql_name"]: [c["sql_name"] for c in t["definition"]["columns"]] for t in schema}}


def questions(requests: list[dict], schema: list[dict]) -> dict:
    q = {}
    for r in requests:
        rid, st, said = r["id"], r["statement"], f"request {r['id']} ('{r['text']}')"
        if st in MAIN:
            q[f"{rid}_main"] = choice(CONTEXT, f"Which table does {said} {'read' if st == 'select' else 'change'}?",
                                      options(requests, r, schema, "main"), RULES)
        if same_rows(r):
            q[f"{rid}_same"] = yes_no(CONTEXT, f"Does {said} work on the same records request {r['waits_for']} "
                                               f"returned or changed (keeping, filtering, sorting or showing them)?",
                                      SAME_RULES)
        if st == "insert":
            q[f"{rid}_rows"] = choice(CONTEXT, f"Are the values of the new rows of {said} written in the prompt, or "
                                               f"copied from existing rows (and from where)?",
                                      options(requests, r, schema, "rows"), RULES)
        if st == "create_table":
            q[f"{rid}_fill"] = choice(CONTEXT, f"What fills the new table of {said}?",
                                      options(requests, r, schema, "fill"), RULES)
        if st in USES:
            for t in schema:
                d = t["definition"]
                q[f"{rid}_uses_{d['table_id']}"] = yes_no(
                    CONTEXT, f"Does {said} use information stored in the {d['sql_name']} table (columns: "
                             f"{', '.join(c['sql_name'] for c in d['columns'])})?", USE_RULES)
    return q


def node(key: str | None) -> dict | None:
    if key is None or key in ("values", "empty", "unclear"):
        return None
    if key.endswith("_result"):
        return {"type": "request_result", "request_id": key.removesuffix("_result")}
    if key.endswith("_new_table"):
        return {"type": "new_table", "request_id": key.removesuffix("_new_table")}
    return {"type": "database_table", "reference": key}


def build(requests: list[dict], answers: dict, schema: list[dict], values: list[dict] = ()) -> dict:
    """The section from Jev's answers. requests: steps 1–2's, each {"id", "text", "waits_for", "statement"}."""
    out, warnings = {}, []

    def pick(rid, key, problem):
        c, probs = answers.get(key, (None, {}))
        p = probs.get(c, 0)
        if c == "unclear" or c is None:                       # "unclear" asks the user: a question, not an option
            warnings.append({"request": rid, "problem": f"{problem}: unclear"}
                            | question(answers, key, skip=["unclear"]))
        elif p < SURE:
            out[rid]["flagged"] = True
            warnings.append({"request": rid, "problem": f"{problem} uncertain", "answer": c, "probability": round(p, 2)}
                            | question(answers, key))
        return c

    for r in requests:
        rid, st = r["id"], r["statement"]
        out[rid] = {}
        if st in MAIN:
            out[rid][MAIN[st]] = node(pick(rid, f"{rid}_main", "table"))
        if same_rows(r):
            p = answers.get(f"{rid}_same", 0.0)
            if p >= 0.5:                                     # the table question no longer matters
                out[rid]["from"] = {"type": "request_result", "request_id": r["waits_for"]}
                out[rid].pop("flagged", None)
                warnings[:] = [w for w in warnings if not (w["request"] == rid and w["problem"].startswith("table"))]
            if 1 - SURE < p < SURE:
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": "same rows uncertain", "probability": round(p, 2)}
                                | question(answers, f"{rid}_same"))
        if st == "insert":
            out[rid]["query_from"] = node(pick(rid, f"{rid}_rows", "rows from"))
        if st == "create_table":
            out[rid]["target"] = {"type": "new_table", "request_id": rid}
            out[rid]["query_from"] = node(pick(rid, f"{rid}_fill", "filled from"))
        if st in USES:
            main = (out[rid].get(MAIN[st]) or {}).get("reference")
            reads = []
            for t in schema:
                tid = t["definition"]["table_id"]
                p = answers.get(f"{rid}_uses_{tid}", 0.0)
                found = [v for v in stored(values) if v["request"] == rid and v["column"].split(".")[0] == tid]
                if (p >= 0.5 or found) and tid != main:     # SQLite found a value there: no question left
                    reads.append(tid)
                    if any(v.get("flagged") for v in found):
                        warnings.append({"request": rid, "problem": "reads a table for a value to confirm (step 3)",
                                         "table": tid, "values": [v["text"] for v in found if v.get("flagged")]})
                if 0.5 <= p < SURE and tid != main and not found:   # leaning no: not read, unasked (a condition
                    # needing it then has no column to read, and the ledger refuses its value; 3 of 3 on set G)
                    warnings.append({"request": rid, "problem": "uses table uncertain", "table": tid,
                                     "probability": round(p, 2)} | question(answers, f"{rid}_uses_{tid}"))
            src = out[rid].get("from") or {}
            if src.get("type") == "request_result":           # what the earlier rows came from is in them already
                earlier = out.get(src["request_id"], {})            # a select's table, or an update's / delete's
                carried = (earlier.get("from") or earlier.get("target") or {}).get("reference")
                reads = [t for t in reads if t != carried]
            out[rid]["reads"] = reads
        if st == "other_object":
            warnings.append({"request": rid, "problem": "an index, a view or a trigger: not supported yet"})
    return {"requests": out, "warnings": warnings}


async def run(jev, prompt: str, requests: list[dict], schema: list[dict], references: list[dict] = (),
              values: list[dict] = ()) -> dict:
    try:
        answers = await ask(jev, state(prompt, requests, schema, references, values), questions(requests, schema))
    except Exception as e:                                    # API error: nothing is guessed
        answers = {"error": repr(e)}
    section = build(requests, answers, schema, values)
    section["jev"] = answers
    return section
