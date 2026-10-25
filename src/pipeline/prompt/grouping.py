"""Step 6 of prompt → AST, stage 4: the conditions of stage 2 joined into one tree per request (all · any), and a
read's sort, limit and group columns (plan S5, S7). Stages 1–2 are conditions.py, stage 3 formula.py.

    code  the conditions of a request, a duplicate merged (reserved > quantity is quantity < reserved); one condition
          is the tree; two or more: each shown as readable SQL
    Jev   one call per prompt: for each pair of a request's conditions, AND or OR (where they meet in the tree);
          for a read stage 1 found sorted / limited / grouped: the sort column and its direction, the limit (a number of the
          request, read by the parameters algorithm, or one for a singular "the cheapest variant"), the group column
    code  the AST: the one tree the pairs' answers fit (tree_of), {"type": "all" | "any", "children"}, or the single
          condition; order_by [{expression, direction}];
          limit; group_by [expression]; below 0.7 an answer is kept and flagged
    ponytail: one sort column (a second one, "by brand then price", when a case needs it); n·(n-1)/2 pair questions
    (15 for six conditions, in the one call)

Section "grouping" of cases/<id>.json: {"requests": {"r1": {"where": AST | None, "rows_from"?, "order_by"?,
"limit"?, "group_by"?, "flagged"?}}, "warnings", "jev"}.
"""
import json
import re
from itertools import combinations

from text_to_num import text2num

from . import SURE
from .asking import question
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


def pairs(n: int) -> list[tuple[int, int]]:
    return list(combinations(range(n), 2))


def components(idx, linked) -> list[list[int]]:
    out, left = [], list(idx)
    while left:
        group, todo = [], [left.pop(0)]
        while todo:
            group.append(i := todo.pop())
            todo += [j for j in left if linked(i, j)]
            left = [j for j in left if j not in todo]
        out.append(sorted(group))
    return out


def tree_of(idx, both):
    """The one tree whose every pair of conditions meets at an AND node exactly when both(i, j) (a cotree): split into
    the groups no AND reaches across (OR of them), else the groups no OR reaches across (AND of them). None when the
    answers fit no tree (a, b, c, d with only a-b, b-c, c-d AND)."""
    if len(idx) == 1:
        return idx[0]
    for kind, linked in (("any", both), ("all", lambda i, j: not both(i, j))):
        if len(groups := components(idx, linked)) > 1:
            kids = [tree_of(g, both) for g in groups]
            return None if None in kids else (kind, kids)
    return None


def build_tree(shape, conds):
    kind, children = shape
    return {"type": kind, "children": [build_tree(c, conds) if isinstance(c, tuple) else conds[c] for c in children]}


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
            for i, j in pairs(len(ps)):                       # one local question per pair: the tree follows
                q[f"{rid}|{i}|{j}"] = choice(
                    CONTEXT, f"In {said}, take two of its conditions: {texts[i]} · {texts[j]}. Does every record "
                             "the request keeps meet both, or can a record be kept that meets only one of them?",
                    {"and": "Every record kept meets both: wherever they sit in the request, they are joined by AND.",
                     "or": "A record meeting only one of them can be kept: they are, or belong to, alternatives "
                           "joined by OR."}, TREE_RULES)
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

    def pick(rid, name, labels=None, what=None):
        c, probs = answers.get(f"{rid}|{name}", (None, {}))
        if c is not None and probs.get(c, 1) < SURE:
            out[rid]["flagged"] = True
            warnings.append({"request": rid, "problem": f"{what or name} uncertain", "answer": c,
                             "probability": round(probs.get(c, 0), 2)} | question(answers, f"{rid}|{name}", labels))
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
            names = [p["column"].split(".c_")[-1] for p in ps]
            said = {(i, j): pick(rid, f"{i}|{j}", what=f"how {names[i]} and {names[j]} combine")
                    for i, j in pairs(len(conds))}
            shape = None if None in said.values() else tree_of(list(range(len(conds))), lambda i, j: said[
                min(i, j), max(i, j)] == "and")
            if shape is None:                                 # no answer, or answers no tree fits: every condition,
                out[rid]["flagged"] = True                    # the narrowest reading
                warnings.append({"request": rid, "problem": "how the conditions combine: no tree fits, AND kept"})
                shape = ("all", list(range(len(conds))))
            out[rid]["where"] = build_tree(shape, conds)
        if g.get("sorted"):
            col, d = pick(rid, "sort"), pick(rid, "direction")
            if col:
                out[rid]["order_by"] = [{"expression": _col(col), "direction": d or "asc"}]
        if g.get("limited"):
            nums = limits(r["text"], params.get(rid, []))
            n = pick(rid, "limit", {f"n{i}": t for i, (t, _) in enumerate(nums)})
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
