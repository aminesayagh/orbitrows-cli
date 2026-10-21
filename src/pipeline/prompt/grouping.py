"""Step 6 of prompt → AST, stage 3: the conditions of stage 2 joined into one tree per request (all · any), and a
read's sort, limit and group columns (plan S5, S7). Stages 1–2 are conditions.py.

    code  the conditions of a request, a duplicate merged (reserved > quantity is quantity < reserved); one condition
          is the tree; two or more: every candidate tree (all of them, any of them, and for three the mixed ones),
          each shown as readable SQL
    Jev   one call per prompt: which tree the request means (only when there are two or more conditions); for a read
          stage 1 found sorted / limited / grouped: the sort column and its direction, the limit (a number of the
          request, read by the parameters algorithm, or one for a singular "the cheapest variant"), the group column
    code  the AST: {"type": "all" | "any", "children"} or the single condition; order_by [{expression, direction}];
          limit; group_by [expression]; below 0.7 an answer is kept and flagged
    ponytail: one sort column (a second one, "by brand then price", when a case needs it); trees of up to three
    conditions (more is all / any only)

Section "grouping" of cases/<id>.json: {"requests": {"r1": {"where": AST | None, "rows_from"?, "order_by"?,
"limit"?, "group_by"?, "flagged"?}}, "warnings", "jev"}.
"""
import json
import re
from itertools import combinations

from text_to_num import text2num

from . import SURE
from orbitrows.sql.sql import program
from orbitrows.services.model.client import ask, choice
from .conditions import tables_of
from .parts import columns_of
from orbitrows.units.header import NAME_LOCALES

CONTEXT = ("A merchant wrote a prompt to edit or read their store database (tables and columns in `tables`). It was "
           "split into the requests in `requests`; each request's conditions are found. Each request now needs how "
           "its conditions combine, and for a read, its order, limit and grouping.")
TREE_RULES = [
    "'and', 'with', a comma or two descriptions of the same records ('black variants under 300') join conditions "
    "with AND: every condition must hold.",
    "'or', 'either … or' between two different conditions joins them with OR: one of them is enough.",
]
DIRECTION = {"asc": "Lowest first: 'the cheapest', 'oldest first', 'shortest', 'A to Z', 'by price'.",
             "desc": "Highest first: 'the most expensive', 'newest first', 'longest', 'top', 'best-selling'."}


def readable(schema, table, cond) -> str:
    """A condition as SQL text, for Jev ("color IN ('black', 'navy')")."""
    sql = program([{"id": "q", "type": "select", "from": {"type": "database_table", "reference": table, "alias": table},
                    "columns": [{"expression": {"type": "aggregate", "function": "count"}, "alias": "n"}],
                    "scope": {"type": "filtered", "where": cond}}], schema)[0]
    return sql.split(" WHERE ", 1)[1].replace(f'"{table}".', "")


def canonical(cond: dict) -> str:
    """Column against column written one way only: quantity < reserved is reserved > quantity."""
    flip = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "!=": "!="}
    if cond.get("type") == "comparison" and cond["right"].get("type") == "column" and \
            json.dumps(cond["left"], sort_keys=True) > json.dumps(cond["right"], sort_keys=True):
        cond = {**cond, "operator": flip[cond["operator"]], "left": cond["right"], "right": cond["left"]}
    return json.dumps(cond, sort_keys=True)


def merged(predicates: list[dict]) -> list[dict]:
    seen, out = set(), []
    for p in predicates:
        if (k := canonical(p["condition"])) not in seen:
            seen.add(k)
            out.append(p)
    return out


def trees(n: int) -> dict:
    """Candidate trees over conditions 0…n-1: {"all": ("all", [0, 1]), …}; mixed ones for three conditions."""
    idx = list(range(n))
    out = {"all": ("all", idx), "any": ("any", idx)}
    if n == 3:
        for pair in combinations(idx, 2):
            (rest,) = [i for i in idx if i not in pair]
            out[f"any{pair}_all{rest}"] = ("all", [("any", list(pair)), rest])
            out[f"all{pair}_any{rest}"] = ("any", [("all", list(pair)), rest])
    return out


def build_tree(shape, conds):
    kind, children = shape
    return {"type": kind, "children": [build_tree(c, conds) if isinstance(c, tuple) else conds[c] for c in children]}


def describe(shape, texts):
    kind, children = shape
    parts = [f"({describe(c, texts)})" if isinstance(c, tuple) else texts[c] for c in children]
    return f" {'AND' if kind == 'all' else 'OR'} ".join(parts)


def state(prompt, requests, schema, section) -> dict:
    return {"prompt": prompt,
            "requests": {r["id"]: {"text": r["text"], "statement": r["statement"]} for r in requests},
            "tables": {t["definition"]["sql_name"]: [c["sql_name"] for c in t["definition"]["columns"]] for t in schema}}


def questions(requests, schema, sources, section, params) -> dict:
    q = {}
    for r in requests:
        rid, said = r["id"], f"request {r['id']} ('{r['text']}')"
        g = section["requests"].get(rid)
        if not g:
            continue
        ps = merged(g.get("predicates", []))
        if len(ps) > 1:
            texts = [readable(schema, p["column"].split(".")[0], p["condition"]) for p in ps]
            q[f"{rid}|tree"] = choice(CONTEXT, f"How do the conditions of {said} combine?",
                                      {k: describe(shape, texts) for k, shape in trees(len(ps)).items()}, TREE_RULES)
        cols = columns_of(schema, tables_of(requests, sources, rid))
        if g.get("sorted"):
            q[f"{rid}|sort"] = choice(CONTEXT, f"By which column does {said} order its records?", cols)
            q[f"{rid}|direction"] = choice(CONTEXT, f"In which direction does {said} order them?", DIRECTION)
        if g.get("limited"):
            nums = {f"n{i}": f"'{t}' ({v})" for i, (t, v) in enumerate(limits(r["text"], params.get(rid, [])))}
            q[f"{rid}|limit"] = choice(CONTEXT, f"How many records does {said} keep?",
                                       nums | {"one": "One record: a singular ('the cheapest variant').",
                                               "none": "None of these: the number is not written here."})
        if g.get("grouped"):
            q[f"{rid}|group"] = choice(CONTEXT, f"Which column does {said} group its records by ('per brand')?", cols)
    return q


def limits(text: str, params: list[dict]) -> list[tuple[str, int]]:
    """Numbers a limit may be: the parameters', and the number words text2num reads anywhere in the request (GLiNER2
    may not propose "two" in "the two heaviest")."""
    out = [(p["text"], int(p["value"])) for p in params if p["value"] is not None and float(p["value"]).is_integer()]
    for w in re.findall(r"[^\W\d_]+(?:-[^\W\d_]+)*", text):
        for lang in NAME_LOCALES:
            try:
                v = text2num(w.casefold(), lang)
            except (ValueError, KeyError, IndexError, NotImplementedError):
                continue
            if v >= 1 and all(t.casefold() != w.casefold() for t, _ in out):
                out.append((w, v))
            break
    return out


def _col(key):
    t, c = key.split(".")
    return {"type": "column", "source": t, "reference": c}


def build(requests, section, params, answers) -> dict:
    out, warnings = {}, []

    def pick(rid, name):
        c, probs = answers.get(f"{rid}|{name}", (None, {}))
        if c is not None and probs.get(c, 1) < SURE:
            out[rid]["flagged"] = True
            warnings.append({"request": rid, "problem": f"{name} uncertain", "answer": c,
                             "probability": round(probs.get(c, 0), 2)})
        return c

    for r in requests:
        rid = r["id"]
        g = section["requests"].get(rid)
        if not g:
            continue
        out[rid] = {**({"rows_from": g["rows_from"]} if g.get("rows_from") else {})}
        ps = merged(g.get("predicates", []))
        conds = [p["condition"] for p in ps]
        if len(conds) <= 1:
            out[rid]["where"] = conds[0] if conds else None
        else:
            shape = trees(len(conds)).get(pick(rid, "tree"))
            if shape is None:                                 # no answer: every condition, the narrowest reading
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": "how the conditions combine: no answer, AND kept"})
                shape = ("all", list(range(len(conds))))
            out[rid]["where"] = build_tree(shape, conds)
        if g.get("sorted"):
            col, d = pick(rid, "sort"), pick(rid, "direction")
            if col:
                out[rid]["order_by"] = [{"expression": _col(col), "direction": d or "asc"}]
        if g.get("limited"):
            n = pick(rid, "limit")
            nums = limits(r["text"], params.get(rid, []))
            value = 1 if n == "one" else nums[int(n[1:])][1] if n and n[1:].isdigit() and int(n[1:]) < len(nums) else None
            if value is None:
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": "limit: no number found"})
            else:
                out[rid]["limit"] = value
        if g.get("grouped"):
            col = pick(rid, "group")
            if col:
                out[rid]["group_by"] = [_col(col)]
    return {"requests": out, "warnings": warnings}


async def run(jev, prompt, requests, schema, sources: dict, section: dict, params: dict) -> dict:
    """section: conditions.run's section (stages 1–2); params: its section["parameters"]."""
    try:
        answers = await ask(jev, state(prompt, requests, schema, section),
                            questions(requests, schema, sources, section, params))
    except Exception as e:                                    # API error: nothing is guessed
        answers = {"error": repr(e)}
    out = build(requests, section, params, answers)
    out["jev"] = answers
    return out
