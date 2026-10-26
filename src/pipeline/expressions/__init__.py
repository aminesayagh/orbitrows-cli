"""Expressions: a computation written in a request's words → one checked expression tree, or the questions it needs.
A module of its own (2026-10-26), used by step 6's stage 3 (the new value of a changed column, prompt/formula.py) and
meant for every place a request computes a value (an output, a sort key, a condition's side). Each step of the
computation is an operation of the catalog (pipeline/operations), written by its builder; this module finds the
steps, orders and links them, and checks the result: no cycle, each result used once, one final value. A weak
answer or a broken map is a question, never a guess.

    words.py   the request's words as spaCy reads them, its numbers, its segments (one per step), column mentions
    steps.py   pass 1: one record per step (what each number, text or date does: a catalog entry; its column)
    links.py   pass 2: which result feeds which (Jev), the checks, the tree (each step by its catalog builder)

    compose(jev, text, context) → {"pass_1": {"records", "ambiguities", "answers"},
                                   "pass_2": {"status": "ok" | "ask", "set": tree, "map", "questions", "flagged",
                                              "answers", "repaired"?}}
    context    what the caller knows of the computation (offsets in `text`):
                 target         the column the expression computes ("t_x.c_y"), its current value the start
                 target_kind    number · text · date
                 words          [{"text", "start", "end"}] the phrases that may name a column (step 3's)
                 predicates     [{"id", "node", "sql", "start", "end", …}] the request's conditions (a branch's)
                 units          [{"start", "end", "value"}] numbers with a unit, read in the target's unit
                 table_columns  {column id: name} the columns a step may start from
                 stored_values  a text target's values when it has few (a status's)
                 texts          [{"text", "start"}] the texts the request writes
                 target_use     what the target is for the request, when it is not the changed column (a read's
                                computed value: "the column the computed value starts from")
                 rules          more rules for what a number does, from the caller (a read's: a number of records
                                to keep is none)
                 (a number pass 1 surely calls none is never a lost step: "the 3 variants", "one of the products")
"""
from orbitrows.pipeline.prompt import SURE
from .links import resolve
from .steps import extract


def positive(cond):
    """A branch condition names the records that get the special formula: positive by definition. Step 6, which knows
    nothing of branches, may have negated it ("but only 10% for the Volt products" read as an exclusion): its
    negation is removed (not X → X; X IS NULL OR NOT X → X)."""
    if cond.get("type") == "not":
        return cond["operand"]
    if cond.get("type") == "any" and len(cond["children"]) == 2:
        nul = [c for c in cond["children"] if c.get("type") == "null_check"]
        neg = [c for c in cond["children"] if c.get("type") == "not"]
        if nul and neg:
            return neg[0]["operand"]
    return cond


async def compose(jev, text: str, context: dict) -> dict:
    target, preds = context["target"], context.get("predicates", [])
    p1 = await extract(jev, text, context)
    branch_ids = {x["left"]["pred"] for x in p1["records"] if x["role"] == "branch_condition"}
    pos = [p | {"sql": positive(p["node"])} if p["id"] in branch_ids else p for p in preds]
    outside = [(o["start"], o["end"]) for o in p1.get("occurrences", [])   # a number Jev surely calls none is not
               if (a := p1["answers"].get(f"{o['occ']}_kind")) and a[0] == "none" and a[1].get("none", 0) >= SURE]
    p2 = await resolve(jev, text, {"target": target, "predicates": pos, "outside": outside}, p1["records"])
    weak = [x for x in p1["records"] if (x["role"] == "where" and p1["answers"].get(
                f"{x['left']['pred']}_role", (None, {}))[1].get("where", 1) < SURE) or x["role"] == "unresolved_condition"]
    two_values = sum(1 for x in p1["records"] if x["function"] == "set") >= 2 or any(
        "several results are used by no other step" in q["problem"] for q in p2["questions"])
    if p2["status"] == "ask" and len(weak) == 1 and two_values:   # two final values for one column
        # A47: "active, but archived for the Orbit products": two final values need a branch; the condition
        # read as a filter at 0.52 is the branch (reported as a repair)
        w = weak[0]
        recs = [x for x in p1["records"] if x is not w] + [
            w | {"role": "branch_condition", "rule_id": "repair.two_values_one_condition"},
            w | {"role": "branch", "function": "case", "rule_id": "repair.two_values_one_condition"}]
        recs.sort(key=lambda x: x["start"])
        pos = [p | {"sql": positive(p["node"])} if p["id"] == w["left"]["pred"] else p for p in pos]
        again = await resolve(jev, text, {"target": target, "predicates": pos, "outside": outside}, recs)
        if again["status"] == "ok":
            p1 = {**p1, "records": recs}
            p2 = again | {"repaired": again.get("repaired", []) + [f"'{w['text']}' read as a branch: two final "
                                                                 f"values and one weak filter"]}
    return {"pass_1": p1, "pass_2": p2}
