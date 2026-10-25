"""Step 6 of prompt → AST, stage 3 · numeric changes (2026-10-15; the PoC's step 9, run after grouping there): pass 1
(operations_jev.py) + pass 2 (operations_map.py) inside the pipeline, in place of step 5's templates for number columns
(text and dates keep the templates).

For each update request and each number column step 5 changes: the request's words, step 3's column words (role
not_value, their column asked in pass 1's call), step 6's conditions (with the words they came from) and step 6's
numbers with units → pass 1 records → pass 2 map → the formula replaces step 5's value. A condition pass 1 calls a
branch leaves step 6's WHERE (it lives in the CASE); an unclear one, or a map pass 2 cannot close, is a question and
step 5's template value stays.
"""
from . import SURE
from .asking import question
from .conditions import column_info, fits
from .grouping import readable
from .operations_jev import extract
from .operations_map import resolve
from .parts import coltype, parents

NUMERIC = ("INTEGER", "REAL")
_KINDS = {}


def stored_values(schema, db, key, most=12) -> list[str]:
    """The column's distinct values when there are few (a status, a colour): the values a change may pick from."""
    d = {t["definition"]["table_id"]: t["definition"] for t in schema}
    tid, cid = key.split(".")
    name = next(c["sql_name"] for c in d[tid]["columns"] if c["column_id"] == cid)
    vals = [v for (v,) in db.execute(f'SELECT DISTINCT "{name}" FROM "{d[tid]["sql_name"]}" WHERE "{name}" IS NOT NULL '
                                     f'LIMIT {most + 1}') if isinstance(v, str)]
    return vals if len(vals) <= most else []


def column_kind(schema, db, key) -> str:
    """number · date · text. A TEXT column whose stored values are all dates (YYYY-MM-DD) is a date column; read once
    per store and process (store-only work, not per prompt)."""
    if key in _KINDS:
        return _KINDS[key]
    typ, _ = coltype(schema, key)
    kind = "number" if typ in NUMERIC else "text"
    if typ == "TEXT":
        d = {t["definition"]["table_id"]: t["definition"] for t in schema}
        tid, cid = key.split(".")
        name = next(c["sql_name"] for c in d[tid]["columns"] if c["column_id"] == cid)
        vals = [v for (v,) in db.execute(f'SELECT DISTINCT "{name}" FROM "{d[tid]["sql_name"]}" WHERE "{name}" IS NOT '
                                         f'NULL LIMIT 50')]
        if vals and all(isinstance(v, str) and len(v) == 10 and v[4] == "-" and v[7] == "-" and v[:4].isdigit()
                        for v in vals):
            kind = "date"
    _KINDS[key] = kind
    return kind


def table_columns(schema, main, numeric=True) -> dict:
    """The columns a step may name: numeric ones for a number change, text ones for a text or date change."""
    d = {t["definition"]["table_id"]: t["definition"] for t in schema}
    out = {}
    for tid in [main] + parents(schema, main):
        for c in d[tid]["columns"]:
            if (c["sql_type"] in NUMERIC) == numeric and not c["column_id"].endswith("_id"):
                out[f"{tid}.{c['column_id']}"] = f"the {c['sql_name']} column of {d[tid]['sql_name']}"
    return out


def predicate_spans(cond6, values, params, rid, off, schema, main):
    """Each step-6 condition with the words it came from: a lookup value of its column (step 3), or the number it
    compares with (step 6's parameters); offsets in the request text."""
    out = []
    for i, p in enumerate(cond6.get("predicates", [])):
        span = next(((v["start"], v["end"]) for v in values if v["request"] == rid and v.get("column") == p["column"]
                     and v.get("start") is not None), None)
        right = (p.get("condition") or {}).get("right") or {}
        if span is None and right.get("type") == "literal":
            span = next(((x["start"], x["end"]) for x in params if x["value"] == right.get("value")), None)
        label = readable(schema, p["column"].split(".")[0], p["condition"])   # on the condition's own table
        item = {"id": f"p{i + 1}", "text": label, "sql": p["condition"], "node": p["condition"]}
        if span:
            item |= {"start": span[0] - off, "end": span[1] - off}
        out.append(item)
    return out


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


def asked(doubt) -> bool:
    """A weak answer of pass 1 or 2 is a question for the user; a doubt flagged at any probability ("the change starts
    from …: to confirm", "not one of the existing values") is a notice: the user's answer would not lift it."""
    return doubt.get("status") != "flagged" or doubt.get("problem") == "answer kept but uncertain"


def to_dml(n):
    """Pass 2's tree → the AST's nodes (a case becomes branches + else)."""
    if not isinstance(n, dict):
        return n
    if n.get("type") == "case":
        return {"type": "case", "branches": [{"when": n["when"], "then": to_dml(n["then"])}], "else": to_dml(n["else"])}
    return {k: ([to_dml(x) for x in v] if isinstance(v, list) else to_dml(v)) for k, v in n.items()}


def without(where, conds):
    """The WHERE tree without the conditions that became branches."""
    if where is None or where in conds:
        return None
    if where.get("type") == "all":
        kids = [c for c in (without(k, conds) for k in where["children"]) if c is not None]
        return None if not kids else kids[0] if len(kids) == 1 else {**where, "children": kids}
    return where


async def run(jev, prompt, requests, schema, sources, values, parts, conditions, grouping, db=None) -> dict:
    """… → {"requests", "warnings", "branches": {request: the conditions that became a CASE's branches}}: the CLI's
    pipeline runs this before grouping, which then leaves them out of the WHERE's tree (grouping: {"requests": {}})."""
    out, warnings, found = {}, [], {}
    for r in requests:
        rid, part = r["id"], parts.get(r["id"]) or {}
        if r.get("statement") != "update":
            continue
        # number columns (no template: pending) and, with a db, text and date columns (the template stays behind)
        assigns = [a for a in part.get("assignments", []) if coltype(schema, a["column"])[0] in NUMERIC
                   or (db is not None and coltype(schema, a["column"])[0] == "TEXT")]
        main = ((sources.get(rid) or {}).get("target") or {}).get("reference")
        if not assigns or not main:
            continue
        off, text = r["start"], prompt[r["start"]:r["end"]]
        cond6 = conditions["requests"].get(rid) or {}
        params = (conditions.get("parameters") or {}).get(rid, [])
        preds = predicate_spans(cond6, values, params, rid, off, schema, main)
        words = [{"text": v["text"], "start": v["start"] - off, "end": v["end"] - off} for v in values
                 if v["request"] == rid and v["role"] in ("not_value", "written") and v.get("start") is not None]
        # "written" too: step 3 may keep "half of the quantity" as one written phrase; Jev says if it names a column
        branches = []
        for a in assigns:
            target = a["column"]
            units = [{"start": x["start"] - off, "end": x["end"] - off, "value": fits(x, column_info(schema, target))}
                     for x in params if (x.get("unit") or {}).get("kind") == "physical_unit"]
            kind = column_kind(schema, db, target) if db is not None else "number"
            up = {"target": target, "target_kind": kind, "words": words, "predicates": preds, "units": units,
                  "table_columns": table_columns(schema, main, kind == "number"),
                  "stored_values": stored_values(schema, db, target) if kind == "text" else [],
                  "texts": [{"text": v["text"], "start": v["start"] - off} for v in values
                            if v["request"] == rid and v["role"] == "written" and v.get("start") is not None]}
            p1 = await extract(jev, text, up)
            branch_ids = {x["left"]["pred"] for x in p1["records"] if x["role"] == "branch_condition"}
            pos = [p | {"sql": positive(p["node"])} if p["id"] in branch_ids else p for p in preds]
            p2 = await resolve(jev, text, {"target": target, "predicates": pos}, p1["records"])
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
                again = await resolve(jev, text, {"target": target, "predicates": pos}, recs)
                if again["status"] == "ok":
                    p1 = {**p1, "records": recs}
                    p2 = again | {"repaired": again.get("repaired", []) + [f"'{w['text']}' read as a branch: two final "
                                                                         f"values and one weak filter"]}
            key = f"{rid}.{target}"
            out[key] = {"records": p1["records"], "pass1_ambiguities": p1["ambiguities"], "map": p2.get("map"),
                        "status": p2["status"], "questions": p2.get("questions", []), "flagged": p2.get("flagged", [])}
            mine, my_branches = [], []                      # applied only if this formula is used (or nothing
            doubts = [(x, calls) for xs, calls in ((p1["ambiguities"], p1["answers"]),   # stands behind it), each
                                                   (p2.get("flagged", []), p2.get("answers", {})))   # with its call
                      for x in xs if x.get("status") == "flagged" or x.get("probability", 1) < SURE]
            for x, calls in doubts:                         # one warning per weak answer: its own question
                mine.append({"request": rid, "problem": f"formula of {target}: uncertain answers ("
                             f"{x.get('question') or x.get('occurrence')}={x.get('answer')} {x.get('probability')})"}
                            | (question(calls, x.get("question") or f"{x.get('occurrence')}_kind", skip=["merchant"])
                               if asked(x) else {}))
            for x in p1["records"]:
                if x["role"] == "unresolved_condition":
                    mine.append({"request": rid, "problem": f"does '{x['text']}' choose the records, or a "
                                                            f"different formula for them?"}
                                | question(p1["answers"], f"{x['left']['pred']}_role", skip=["unresolved_condition"]))
                if x["role"] == "branch_condition":
                    my_branches.append(next(p["node"] for p in preds if p["id"] == x["left"]["pred"]))
            out[key]["kind"] = kind
            if p2["status"] != "ok" and not a.get("pending"):
                out[key]["used"] = "template"               # text / date: the template's value stays, as before
                continue
            warnings += mine
            branches += my_branches
            if p2["status"] != "ok":
                warnings += [{"request": rid, "problem": f"formula of {target}: {q['problem']}"}
                             | {k: q[k] for k in ("question",) if k in q} for q in p2["questions"]]
                if a.get("pending"):                        # no template value behind it: not built, asked
                    part["assignments"].remove(a)
                    warnings.append({"request": rid, "problem": f"the change of {target} is not built until the "
                                                                f"questions above are answered"})
                continue                                    # (an old template value, if any, stays, asked)
            value = to_dml(p2["set"])
            if coltype(schema, target)[0] == "INTEGER":     # a whole-number column takes whole values
                value = {"type": "function", "name": "round", "arguments": [value]}
            a["value"], a["from"] = value, "formula"
            a.pop("pending", None)
        if branches:
            found[rid] = branches
        if branches and grouping["requests"].get(rid):     # a branch lives in the CASE, not in the WHERE
            g = grouping["requests"][rid]
            g["where"] = without(g.get("where"), branches)
        if branches:                                        # step 6's doubts about a branch condition no longer apply:
            cols = {p["column"] for p in cond6.get("predicates", []) if p["condition"] in branches}   # positive by
            conditions["warnings"][:] = [w for w in conditions["warnings"] if not (w.get("request") == rid and (
                any(w["problem"] == f"negation of {c} uncertain" for c in cols) or w["problem"] == "all rows uncertain"))]
    return {"requests": out, "warnings": warnings, "branches": found}
