"""Step 2 of prompt → AST: the statement each request needs (decided 2026-10-10: Jev is asked the AST statement type
directly, replacing prompt_edit/family_requests.py's operation × element questions).

    Jev   one call per prompt, two questions per request: the statement type (STATEMENTS) and whether it holds two
          actions (step 1 cuts it again); evidence: the prompt, the requests with step 1's links, the store's tables
          and columns. What a change reports back is what SQLite's execution reports: not asked
    code  below 0.8: the likeliest statement is kept and flagged (the merchant is asked); other_object (an index, a
          view) is not supported yet: a warning, never a guessed AST
    code  step 1's "rows" input becomes request_result (the request it waits for is a select) or affected_rows (it
          inserts, updates or deletes: the executor keeps the ids SQLite returns)

Section "operation" of cases/<id>.json: {"requests": {"r1": {"statement", "several_actions"?, "input"?, "flagged"?}},
"warnings": [...], "jev": {...}}. The examples in the descriptions and rules must not be test prompts (cases/).
"""
from . import SURE
from .asking import question
from orbitrows.services.model.client import ask, choice, yes_no

CONTEXT = ("A merchant wrote a prompt to edit or read their store database (its tables and columns are in `tables`). "
           "The prompt was split into the requests in `requests`; `waits_for` names the request that must be done "
           "first. Each request now needs one kind of database statement.")
STATEMENTS = {
    "select": "It only shows, lists, finds, counts, sums, compares or exports information, and changes nothing.",
    "insert": "It adds new rows: a new record (a supplier, a product, a variant, a stock line) or copies of records.",
    "update": "It changes values in existing rows: it sets, raises, lowers, clears, fills or moves values, or renames "
              "a value; the rows and the columns stay.",
    "delete": "It removes rows (records) from a table.",
    "alter_table": "It changes the structure of an existing table: it adds, renames or removes a column, or renames "
                   "the table.",
    "create_table": "It creates a new table, empty or filled from other data.",
    "drop_table": "It removes a whole table.",
    "other_object": "It creates or changes another kind of database object: an index, a view, a trigger.",
    "application": "It asks the application for something that is not a read or a change of the data: a safety copy "
                   "(backup) of the store, undoing a change, restoring an earlier version, a question about the tool.",
}
RULES = [
    "Choose the request's main statement. When it changes the store and also asks for information back ('delete "
    "the empty listings and tell me how many there were'), the statement is the change (delete).",
    "Selecting rows by a condition or computing from other values is part of a change, not a read: 'lower the price "
    "of the items with a stock over 80' is update.",
    "'add 5 to the stock' changes values (update); 'add a stock column' changes the structure (alter_table); 'add a "
    "brand named Kora' adds a row (insert).",
    "Removing records is delete; emptying values is update ('blank the barcode of the Kodak films': the rows stay); "
    "removing a column is alter_table; removing a whole table is drop_table.",
    "Copying or duplicating records adds rows: insert.",
    "Renaming a value is update ('rename the product Walkman to Walkman II'); renaming a column or a table is "
    "alter_table.",
    "Moving a quantity from one record to another changes values: update.",
    "A copy with a destination written (a new table, a table name: 'copy the French suppliers into a new table', "
    "'a backup copy of the orders called orders_old') is create_table or insert, even when called a backup; a "
    "safety copy with no destination ('take a backup first', 'save the store') is application.",
    "Answer for this request only.",
]
SEVERAL_RULES = [
    # 2026-10-15 (operations step): branches of one change are one action
    "A formula given to some records in place of the others' formula ('…; the Lego sets get 20% off instead', "
    "'…, otherwise add 2', '…; for all the others, take 5 off') is ONE action: one change of the same column "
    "with a different formula for some records, not two actions.",
    "Answer yes when the request asks for two things to be done, in any order: 'before you show me their names, "
    "lower the prices' (show and lower), 'show the new prices once you have lowered them' (lower, then show).",
    "Answer no for one action with its conditions, its limits or when it happens ('lower the price of the items "
    "with a stock over 80', 'never below the cost', 'every Monday'), and for a change that also reports back "
    "('delete the empty listings and tell me how many there were').",
]
INPUT = {"select": "request_result", "insert": "affected_rows", "update": "affected_rows", "delete": "affected_rows"}


def state(prompt: str, requests: list[dict], schema: list[dict]) -> dict:
    return {"prompt": prompt,
            "requests": {r["id"]: {k: r[k] for k in ("text", "waits_for") if r.get(k)} for r in requests},
            "tables": {t["definition"]["sql_name"]: [c["sql_name"] for c in t["definition"]["columns"]] for t in schema}}


def questions(requests: list[dict]) -> dict:
    q = {}
    for r in requests:
        q[f"{r['id']}_statement"] = choice(CONTEXT, f"Which kind of statement does request {r['id']} ('{r['text']}') "
                                                    f"need?", STATEMENTS, RULES)
        q[f"{r['id']}_several"] = yes_no(CONTEXT, f"Does request {r['id']} ('{r['text']}') ask for two or more separate "
                                                  f"actions?", SEVERAL_RULES)
    return q


def build(requests: list[dict], answers: dict) -> dict:
    """The section from Jev's answers. requests: step 1's, each {"id", "text", "waits_for", "input"}."""
    out, warnings = {}, []
    for r in requests:
        rid = r["id"]
        st, probs = answers.get(f"{rid}_statement", (None, {}))
        node = {"statement": st}
        if probs.get(st, 0) < SURE:
            node["flagged"] = True
            warnings.append({"request": rid, "problem": "statement uncertain", "answer": st,
                             "probability": round(probs.get(st, 0), 2)} | question(answers, f"{rid}_statement"))
        several = answers.get(f"{rid}_several", 0.0)
        if several >= 0.5:                                    # step 1 cuts it again (request_graph.recut); if it
            node["several_actions"] = round(several, 2)       # can't, the merchant is asked: never one action dropped
            warnings.append({"request": rid, "problem": "two actions in one request: only one is built",
                             "probability": round(several, 2)})
        if st == "other_object":
            warnings.append({"request": rid, "problem": "an index, a view or a trigger: not supported yet"})
        if st == "application":                               # notice #15: a guardrail in production
            warnings.append({"request": rid, "problem": "not a database request (a backup, an undo, a question "
                                                        "about the tool): nothing is built"})
        out[rid] = node
    for r in requests:                                        # step 1's "rows": which rows, now that statements are known
        if r.get("input") != "rows" or not r.get("waits_for"):
            continue
        before = out.get(r["waits_for"], {}).get("statement")
        if before in INPUT:
            out[r["id"]]["input"] = INPUT[before]
        else:
            out[r["id"]]["input"] = "rows"
            warnings.append({"request": r["id"], "problem": f"takes the rows of {r['waits_for']}, a {before}: no rows"})
    return {"requests": out, "warnings": warnings}


async def run(jev, prompt: str, requests: list[dict], schema: list[dict]) -> dict:
    try:
        answers = await ask(jev, state(prompt, requests, schema), questions(requests))
    except Exception as e:                                    # API error: nothing is guessed
        answers = {"error": repr(e)}
    section = build(requests, answers)
    section["jev"] = answers
    return section
