"""Operations, pass 2 (2026-10-14): reference the unknowns of the pass-1 records and get the map. Every `unresolved`
operand points to another record's result or to the column's current value; a branch gets its then / else; the
assignment gets its final value. Code lists the unknowns and their options, Jev chooses (one call), code checks the
map (no cycle, each result used once, bounds feed limits, one final value) and builds the formula. A weak answer or a
broken map becomes a question for the user, never a guess.

Input: the request text, step 0's annotations (target, predicates with SQL) and pass 1's records.
Output: {"status": "ok" | "ask", "map": {record: {slot: source}}, "set": tree, "where": [conditions], "sql": {...},
         "questions": [...], "flagged": [...]}
The formula tree uses the step-5 nodes (column / literal / arithmetic / function / case); "sql" is the same formula as
SQLite text over `t_table.c_column` ids, for tests.
"""
from orbitrows.services.model.client import ask, choice
from orbitrows.pipeline.operations.builders import BUILDERS
from orbitrows.pipeline.prompt import SURE
from orbitrows.pipeline.prompt.asking import question

MIN = 0.5
VALUE = ("calculation", "rounding", "bound", "guard", "branch")
CONTEXT = ("A user's request changes a number column. It was split into steps (r1, r2, …): each step's own words "
           "are shown. Some steps do not say which value they apply to: link each one to the value it uses.")
RULES = [
    "Follow the order the user MEANS, not the order of the words: in 'apply the 15% only after removing the 5' the "
    "15% uses the result of 'removing the 5'; in 'add 10 to the price after a 20% discount' the +10 uses the discount's "
    "result and the discount uses the current price.",
    "The first step of a formula uses the column's current value. A different formula for some records (a branch) "
    "starts again from the current value unless the sentence says it continues another step.",
    "Rounding 'everything' and the limits (never above / below) apply to the result after the branches and after the "
    "other steps; limits come after rounding.",
    "A limit's value ('twice their cost', 'the cost plus 10%') is the step that computes it.",
    "A step 'combines the formula of these records with the formula of the other records' is where a branch ends: the "
    "steps applying to everything after the branch (rounding everything, limits) use that step, not one of the two "
    "formulas.",
    "A limit (never above / below, 'keep the old price if the new one is lower') applies to the NEW value the other "
    "steps compute, never to the current value. With two limits, the second one uses the first one's result.",
    "With two branch steps, the first one's 'other records' formula is the second branch step; the last branch's "
    "other records keep the current value unless the sentence gives a formula for everything.",
    "Answer that the user must say only when the sentence allows two different readings.",
]


def label(r: dict) -> str:
    f, right = r["function"], r["right"] or {}
    if f and f.startswith("entry:"):                        # a catalog change: what its own builder writes, on
        from orbitrows.pipeline.operations import use      # "value" (the value the step applies to)
        e = catalog_entry(f[6:])
        given = {"column": {"type": "column", "source": "t_", "reference": "c_value"}} | right.get("fill", {})
        if right.get("slot"):
            given[right["slot"]] = right.get("value")
        if right.get("other"):
            given["other"] = col(right["other"]["column"])
        params, problems = use.fill(e, given)
        written = e["name"].lower() if problems else sql(use.build(e, params)).replace("t_.c_value", "value")
        return f"{r['id']}: '{r['text']}' ({written})"
    v = right.get("source") or right.get("value") or right.get("column", "").split(".c_")[-1] or "?"
    what = {"*": f"× {right.get('value', '?')}", "+": f"+ {v}", "-": f"− {v}", "/": f"÷ {v}", "set": f"set to {v}",
            "round": "round", "clamp_max": "never above a limit", "clamp_min": "never below a limit",
            "case": "combines the formula of these records with the formula of the other records"}.get(f, f)
    return f"{r['id']}: '{r['text']}' ({what})"


def unknowns(recs: list[dict], target: str) -> list[tuple[str, dict, str]]:
    """The slots to link. A step naming the target column ("add 10 to the price after a 20% discount") is one too:
    the price it names may be the current value or the result of another step."""
    out = []
    for r in recs:
        left = r["left"] or {}
        if (r["role"] in ("calculation", "rounding", "guard") and (left.get("unresolved") or left.get("column") == target)) \
                or (r["role"] == "bound" and left.get("unresolved")):
            out.append((f"{r['id']}_left", r, "left"))
        if r["role"] in ("calculation", "guard", "bound") and (r["right"] or {}).get("unresolved"):
            out.append((f"{r['id']}_right", r, "right"))
        if r["role"] == "branch":
            out += [(f"{r['id']}_then", r, "then"), (f"{r['id']}_else", r, "else")]
    return out


def questions(recs, todo) -> dict:
    values = [r for r in recs if r["role"] in VALUE]
    q = {}
    for key, r, slot in todo:
        # a limit's value (a bound) is only ever a guard's limit; nothing else applies to it or starts from it
        limit_slot = r["role"] == "guard" and slot == "right"
        opts = {x["id"]: label(x) for x in values if x["id"] != r["id"] and (x["role"] == "bound") == limit_slot
                or (limit_slot and x["id"] != r["id"] and not any(y["role"] == "bound" for y in values))}
        computed = any(x["role"] in ("calculation", "rounding", "branch") for x in values)
        if slot == "else" or (slot == "left" and not (r["role"] == "guard" and computed)):
            # a limit applies to the NEW value: never offered the current one when a new one is computed
            opts["current"] = "The column's current value, before any change."
        opts["user"] = "The sentence allows two readings: the user must say."
        task = {"left": f"Which value does step {r['id']} ('{r['text']}') apply to?",
                "right": f"Which step gives the value step {r['id']} ('{r['text']}') uses as its "
                         f"{'limit' if r['role'] == 'guard' else 'amount'}?",
                "then": f"The records '{r['text']}' get their own formula: which step is the LAST step of it? (In "
                        f"'For Rabat, add 8; otherwise subtract 2' the Rabat records' last step is 'add 8'.)",
                "else": f"The OTHER records (not '{r['text']}'): which step is the last of THEIR formula? Another "
                        f"branch step if other records get a formula of their own, or the current value when they "
                        f"keep it. (In 'For Rabat, add 8; otherwise subtract 2' the others' last step is "
                        f"'subtract 2'.)",
                "final": "Which step gives the final value written to the column?"}[slot]
        q[key] = choice(CONTEXT, task, opts, RULES)
    return q


def check(recs, links) -> list[str]:
    """Problems of the map: cycles, results used twice or never, bounds outside limits, no final value."""
    ids = {r["id"]: r for r in recs}
    problems, used = [], {}
    for (rid, slot), src in links.items():
        if src in ids:
            used.setdefault(src, []).append(f"{rid}.{slot}")
            if ids[src]["role"] == "bound" and not (ids[rid]["role"] == "guard" and slot == "right"):
                problems.append(f"{src} computes a limit but feeds {rid}.{slot}")
    for src, by in used.items():
        if len(by) > 1:
            problems.append(f"the result of {src} is used twice ({', '.join(by)})")
    sinks = [r for r in recs if r["role"] in VALUE and r["id"] not in used]
    if len(sinks) > 1:
        problems.append("several results are used by no other step: "
                        + ", ".join(f"{r['id']} ('{r['text']}')" for r in sinks))
    if not sinks and any(r["role"] in VALUE for r in recs):
        problems.append("no final value")
    if not any(r["role"] in VALUE for r in recs):
        problems.append("no change was found in the request")   # e.g. a misspelt verb pass 1 could not see

    def inputs(rid):
        return [s for (x, _), s in links.items() if x == rid and s in ids]

    def cyclic(rid, seen):
        return rid in seen or any(cyclic(s, seen | {rid}) for s in inputs(rid))
    if any(cyclic(r["id"], set()) for r in recs if r["role"] in VALUE):
        problems.append("the steps form a cycle")
    return problems


def reached(recs, links) -> set:
    """The steps the final formula uses (walking the map from the final value)."""
    last, seen = final(recs, links), set()
    todo = [last] if last else []
    while todo:
        x = todo.pop()
        if x in seen:
            continue
        seen.add(x)
        todo += [s for (rid, _), s in links.items() if rid == x]
        todo += [r["left"]["pred"] for r in recs if r["id"] == x and r["role"] == "branch"]
    return seen


def unused_numbers(text, upstream, recs, links) -> list[str]:
    """Each number of the request must belong to a step the formula uses, or to a condition: a number left out is a
    step lost on the way (pass 1 dropped it, or the map does not use it), so the user is asked."""
    from .words import occurrences
    spans = [(text.find(p["text"]), text.find(p["text"]) + len(p["text"])) for p in upstream.get("predicates", [])]
    spans += [tuple(w) for p in upstream.get("predicates", []) for w in p.get("words", [])]   # the condition's own words
    used = reached(recs, links)
    out = []
    for o in occurrences(text, "en", [s for s in spans if s[0] >= 0]):
        if (o["start"], o["end"]) in upstream.get("outside", []):   # surely not the computation's (a read's limit)
            continue
        steps = [r for r in recs if r["role"] in VALUE and
                 ((r.get("start") is not None and r["start"] <= o["start"] < r["end"])
                  or (r.get("start") is None and (i := text.find(r["text"])) >= 0 and i <= o["start"] < i + len(r["text"])))]
        if not any(r["id"] in used for r in steps):
            out.append(f"the number '{o['text']}' is not used in the formula")
    return out


def possible(recs, links, rid, slot, src) -> bool:
    """Could rid.slot take src, given the other links? Not when it closes a cycle, nor when one formula of a branch
    would take the other formula's result (in "…; Atlas gets 5 more instead", +5 cannot start from the +1)."""
    if src in ("current",):
        return True
    trial = dict(links) | {(rid, slot): src}

    def inputs(x):
        return [s for (y, _), s in trial.items() if y == x and s not in ("current",)]

    def reaches(a, b, seen=()):
        return a == b or (a not in seen and any(reaches(i, b, seen + (a,)) for i in inputs(a)))
    if reaches(src, rid):                                   # src depends on rid: a cycle
        return False
    for b in (r for r in recs if r["role"] == "branch"):
        then, other = trial.get((b["id"], "then")), trial.get((b["id"], "else"))
        if then and other and ((reaches(then, other) and other != "current") or (reaches(other, then) and then != "current")):
            return False                                    # one branch formula built on the other
    return True


def final(recs, links):
    """The final value: the one value no other step uses (several or none: the user is asked)."""
    used = {s for s in links.values()}
    sinks = [r["id"] for r in recs if r["role"] in VALUE and r["id"] not in used]
    return sinks[0] if len(sinks) == 1 else None


# --- the formula ---------------------------------------------------------------------------------------------------

def col(key):
    t, c = key.split(".")
    return {"type": "column", "source": t, "reference": c}


def lit(v):
    return {"type": "literal", "value": v}


ENTRY = {"+": "ADD_AMOUNT", "-": "SUBTRACT_AMOUNT", "*": "MULTIPLY_BY", "/": "DIVIDE_BY", "round": "ROUND_DECIMALS",
         "clamp_max": "CLAMP_MAX", "clamp_min": "CLAMP_MIN"}
# the steps this pass composes itself (a column added or subtracted, a limit computed from a column, a plain
# rounding) → the catalog's entry whose builder writes them; every chosen change is an entry: step (step 1.3)
_ENTRIES = {}


def catalog_entry(entry_id: str) -> dict:
    if not _ENTRIES:
        from orbitrows.pipeline.operations.catalog import connect, entries
        _ENTRIES.update({e["id"]: e for e in entries(connect())})
    return _ENTRIES[entry_id]


def build(recs, links, target, preds) -> dict:
    ids = {r["id"]: r for r in recs}

    def operand(o, r, slot):
        src = links.get((r["id"], slot))
        if src == "current":
            return col(target)
        if src in ids:
            return node(ids[src])
        if o and "column" in o:
            return col(o["column"])
        if o and "value" in o:
            return lit(o["value"])
        if o and "pred" in o:
            return None
        raise ValueError(f"{r['id']}.{slot} has no source")

    def node(r):
        f = r["function"]
        if r["role"] == "branch":
            return {"type": "case", "when": preds[r["left"]["pred"]], "then": operand(None, r, "then"),
                    "else": operand(None, r, "else")}
        if f == "set":
            return operand(r["right"], r, "right")
        if f.startswith("entry:"):                          # a catalog change (step 1.3): its own declaration
            from orbitrows.pipeline.operations import use
            e, right = catalog_entry(f[6:]), r["right"] or {}
            given = {"column": operand(r["left"], r, "left") if r["left"] else col(target)} | right.get("fill", {})
            if right.get("slot"):                           # the number as written, or the step that computes it
                given[right["slot"]] = node(ids[links[(r["id"], "right")]]) if (r["id"], "right") in links \
                    else right["value"]
            if right.get("other"):
                given["other"] = col(right["other"]["column"])
            params, problems = use.fill(e, given)
            if problems:
                raise ValueError("; ".join(problems))
            return use.build(e, params)
        if f not in ENTRY:
            raise ValueError(f"unsupported function {f}")
        e, left, right = catalog_entry(ENTRY[f]), operand(r["left"], r, "left"), r["right"] or {}
        p = {"column": left}
        if f in ("+", "-", "*", "/", "clamp_max", "clamp_min"):   # an amount, a factor, a limit: maybe a step's result
            p[{"+": "amount", "-": "amount", "*": "factor", "/": "divisor"}.get(f, "limit")] = operand(r["right"], r,
                                                                                                       "right")
        elif f == "round":
            p["digits"] = right.get("value", 0)
        return BUILDERS[e["builder_key"]](p, e["config"])

    last = final(recs, links)
    return node(ids[last]) if last else col(target)


def sql(n) -> str:
    """The formula as SQLite text over t_table.c_column ids (for the tests)."""
    t = n["type"]
    if t == "column":
        return f"{n['source']}.{n['reference']}"
    if t == "literal" and n["value"] is None:
        return "NULL"
    if t == "literal":
        return repr(n["value"]) if not isinstance(n["value"], str) else "'" + n["value"].replace("'", "''") + "'"
    if t == "arithmetic":
        return f"({sql(n['left'])} {n['operator']} {sql(n['right'])})"
    if t == "function":
        name = {"least": "MIN", "greatest": "MAX", "round": "ROUND", "coalesce": "COALESCE", "replace": "REPLACE",
                "date": "date", "upper": "UPPER", "lower": "LOWER", "substr": "substr"}.get(n["name"], n["name"].upper())
        return f"{name}({', '.join(sql(a) for a in n['arguments'])})"
    if t == "concat":
        return "(" + " || ".join(sql(x) for x in n["parts"]) + ")"
    if t == "case":
        return f"(CASE WHEN COALESCE(({n['when']}), 0) THEN {sql(n['then'])} ELSE {sql(n['else'])} END)"
    raise ValueError(t)


async def resolve(jev, text: str, upstream: dict, records: list[dict]) -> dict:
    recs = [dict(r) | {"id": f"r{i + 1}"} for i, r in enumerate(records)]
    preds = {p["id"]: p["sql"] for p in upstream.get("predicates", [])}
    todo = unknowns(recs, upstream["target"])
    preset = {}
    shares = [x for x in recs if x["role"] == "calculation" and x["function"] == "*"
              and (x["left"] or {}).get("column") not in (None, upstream["target"])]
    for key, r, slot in todo:
        if slot == "right" and r["role"] == "calculation" and r["function"] in ("+", "-") and len(shares) == 1:
            preset[(r["id"], "right")] = shares[0]["id"]     # "deduct 10% of the cost": the share is the amount
    todo = [t for t in todo if (t[1]["id"], t[2]) not in preset]
    ans = await ask(jev, {"request": text, "steps": [label(r) for r in recs if r["role"] in VALUE]},
                    questions(recs, todo)) if todo else {}
    links, out_q, flagged = dict(preset), [], []
    weak, repaired = [], []
    for key, r, slot in todo:
        pick, probs = ans.get(key, (None, {}))
        p = probs.get(pick, 0)
        if pick is None or p < MIN or pick == "user":
            weak.append((key, r, slot))
            continue
        if p < SURE:
            flagged.append({"question": key, "answer": pick, "probability": round(p, 2)})
        links[(r["id"], slot)] = pick
    for key, r, slot in weak:                               # a weak answer, judged again among the POSSIBLE sources:
        probs = ans.get(key, (None, {}))[1]                 # part of the vote may sit on sources the map cannot take
        ok = {k: v for k, v in probs.items() if k != "user" and possible(recs, links, r["id"], slot, k)}
        best = max(ok, key=ok.get) if ok else None
        if best and probs.get(best, 0) >= 0.3 and ok[best] / sum(ok.values()) >= SURE:
            links[(r["id"], slot)] = best                  # e.g. A41: "current" 0.46, the rest on a cycle / the
            repaired.append(f"{r['id']}.{slot} = {best}: the only possible source "   # other branch's formula
                            f"({round(probs.get(best, 0), 2)} of the answer; the rest would break the map)")
            continue
        pick = ans.get(key, (None, {}))[0]
        out_q.append({"problem": f"which value {r['id']} ('{r['text']}') uses ({slot})", "answer": pick,
                      "probability": round(probs.get(pick, 0), 2)}     # the user's answer, settled, is taken above
                     | question(ans, key, {x["id"]: label(x) for x in recs}, skip=["user"]))
    guards = [r for r in recs if r["role"] == "guard"]
    for a, b in zip(guards, guards[1:]):                    # two limits on the same value: chain them (clamps commute
        if links.get((a["id"], "left")) and links.get((a["id"], "left")) == links.get((b["id"], "left")):   # unless
            links[(b["id"], "left")] = a["id"]              # the limits cross, which SQLite would show)
            repaired.append(f"{b['id']} now uses {a['id']} (two limits on one value)")
    for r in recs:                                          # a condition the step did not resolve is a question
        if r["role"] == "unresolved_condition":
            out_q.append({"problem": f"does '{r['text']}' choose the records or a different formula?"})
    where = [f"COALESCE(({preds[r['left']['pred']]}), 0)" for r in recs if r["role"] == "where"] + \
            [f"NOT COALESCE(({preds[r['left']['pred']]}), 0)" for r in recs if r["role"] == "where_not"]
    problems = check(recs, links) if not out_q else []
    if not out_q and not problems:
        problems += unused_numbers(text, upstream, recs, links)
    out = {"map": {f"{a}.{b}": s for (a, b), s in links.items()}, "answers": ans, "flagged": flagged, "repaired": repaired,
           "questions": out_q + [{"problem": p} for p in problems], "where": where}
    if out["questions"]:
        return out | {"status": "ask"}
    tree = build(recs, links, upstream["target"], preds)
    return out | {"status": "ok", "set": tree, "sql": {"set": sql(tree), "where": " AND ".join(where) or None}}
