"""The formula's links from the sentence's structure (2026-11-02, the typed model): pass 2 asked Jev, one link at a
time, which result feeds each step; the answers didn't fit together ("the result of r6 is used twice", "the steps
form a cycle": set T, 32 of 36 changes refused). Here the code builds the structure, typed, and Jev only says, for an
operation the sentence leaves unclear, which records it applies to.

    clauses    the sentence cut at ";" (sections) and "," (clauses); an operation belongs to the branch condition of
               its clause; a clause without one holds the others' operations; a clause holding only a condition
               ("If a variant costs more than 100, …") opens a scope over the next clauses of its section: the
               conditions there are nested in its formula
    a joint    a clause with one condition and several operations, or several conditions and an operation between
               them: Jev chooses, per operation, among the conditions in scope and "the others"
    the tree   per scope: branch(c1).then = c1's formula, .else = branch(c2) … the last's else = the others'
               operations, or the current value; operations chain in written order from the current value; roundings
               and limits apply after every branch, each limit to its own bound (in order)

    plan(text, recs, target) → {"links": {(id, slot): source}, "joints": {op id: [option ids]}} or None (pass 2's
    questions then: a bound left unresolved, a step of no known kind)
    finish(plan, picks) → links, once each joint's operation has its group
"""
import re

OPS = ("calculation",)
AFTER = ("rounding", "guard")


def plan(text: str, recs: list[dict], target: str, negated=()) -> dict | None:
    branches = sorted((r for r in recs if r["role"] == "branch"), key=lambda r: r["start"])
    ops = sorted((r for r in recs if r["role"] in OPS), key=lambda r: r["start"])
    after = sorted((r for r in recs if r["role"] in AFTER), key=lambda r: r["start"])
    bounds = sorted((r for r in recs if r["role"] == "bound"), key=lambda r: r["start"])
    if not branches or not ops or any(r["role"] not in (*OPS, *AFTER, "bound", "branch", "branch_condition", "where",
                                                        "where_not", "assignment") for r in recs):
        return None
    if any((b["left"] or {}).get("unresolved") for b in bounds) or any(
            (o["right"] or {}).get("unresolved") for o in ops):     # a share of another step ("10% of the cost")
        return None
    cuts = [m.start() for m in re.finditer(r"[;,]", text)] + [len(text)]
    sections = [m.start() for m in re.finditer(r";", text)] + [len(text)]

    def clause(pos):
        return next(i for i, c in enumerate(cuts) if pos <= c)

    def section(pos):
        return next(i for i, c in enumerate(sections) if pos <= c)

    # scopes: a branch alone in its clause (no operation there) opens a scope over the next clauses of its section
    op_clauses = {clause(o["end"] - 1) for o in ops}
    parent = {}
    for b in branches:
        for o in branches:
            if o is not b and clause(o["start"]) not in op_clauses and section(o["start"]) == section(b["start"]) \
                    and clause(o["start"]) < clause(b["start"]):
                parent[b["id"]] = o["id"]                    # the nearest opener before it wins (later in the loop)
    def at(o):                                                # an operation's place: its number, at the end of its
        return o["end"] - 1                                   # words (pass 1's spans may start a clause earlier)
    group, joints = {}, {}
    for b in branches:                                        # "all get 10% except the black ones, which get 5%":
        if b["left"]["pred"] in negated:                      # the excepted clause's operation is the others', the
            c = clause(b["start"])                            # next clause without a condition is the excepted ones'
            nxt = [o for o in ops if clause(at(o)) == c + 1 and section(at(o)) == section(b["start"])]
            if nxt and not any(clause(x["start"]) == c + 1 for x in branches):
                group |= {o["id"]: b["id"] for o in nxt}
                group |= {o["id"]: f"others:{parent.get(b['id'])}" for o in ops if clause(at(o)) == c}
    claimed = attached(text, branches, ops, at)               # "except the black ones, which get 5%"
    for b, mine_ in claimed.items():
        group |= {o: b for o in mine_}
        c = clause(next(x["start"] for x in branches if x["id"] == b))
        group |= {o["id"]: f"others:{parent.get(b)}" for o in ops   # the other operation of its clause: the others'
                  if clause(at(o)) == c and o["id"] not in mine_ and o["id"] not in group}
    for o in ops:
        if o["id"] in group:
            continue
        c = clause(at(o))
        here = [b for b in branches if clause(b["start"]) == c]
        mine = [x for x in ops if clause(at(x)) == c]
        scope = parent.get(here[0]["id"]) if here else next(
            (b["id"] for b in reversed(branches) if clause(b["start"]) not in op_clauses
             and section(b["start"]) == section(at(o)) and clause(b["start"]) < c), None)
        others = f"others:{scope}"
        if not here:
            group[o["id"]] = others
        elif len(here) == len(mine):                          # as many operations as conditions: paired in order
            group[o["id"]] = here[mine.index(o)]["id"]       # ("black by 10% and the white ones by 20%")
        else:                                                 # unclear: Jev says which records it applies to
            joints[o["id"]] = [b["id"] for b in branches if parent.get(b["id"]) == scope] + [others]
    return {"links": {}, "joints": joints, "group": group, "parent": parent, "branches": [b["id"] for b in branches],
            "ops": [o["id"] for o in ops], "after": [a["id"] for a in after], "bounds": [b["id"] for b in bounds],
            "guards": [a["id"] for a in after if a["role"] == "guard"],
            "target": target}


def attached(text, branches, ops, at) -> dict:
    """{branch id: [op ids]}: the operations inside a clause attached to the condition's own noun or clause ("except
    the black ones, which get 5%": a relative clause of "ones"; "unless it is black, in which case raise it by 5%": a
    clause of "is"), from spaCy's parse. An operation two conditions would claim is left to the other rules."""
    from .words import nlp
    doc = nlp("en")(text)
    out, seen = {}, {}
    for b in branches:
        t = next((x for x in doc if x.idx <= b["start"] < x.idx + len(x)), None)
        while t is not None and t.pos_ not in ("NOUN", "PRON", "PROPN") and t.dep_ not in ("advcl",) \
                and t.head.i != t.i:
            t = t.head
        if t is None:
            continue
        inside = {x.i for c in t.children if c.dep_ in ("relcl", "advcl") for x in c.subtree}
        for o in ops:
            tok = next((x for x in doc if x.idx <= at(o) < x.idx + len(x)), None)
            if tok is not None and tok.i in inside:
                seen.setdefault(o["id"], []).append(b["id"])
    for o, bs in seen.items():
        if len(bs) == 1:
            out.setdefault(bs[0], []).append(o)
    return out


def finish(p: dict, picks: dict) -> dict:
    """The links, every operation in its group (picks: the joints' answers, op id → group)."""
    group = p["group"] | picks
    links = {}

    def chain(ids):
        prev = "current"
        for i in ids:
            links[(i, "left")] = prev
            prev = i
        return prev

    def scope_value(scope):
        conds = [b for b in p["branches"] if p["parent"].get(b) == scope]
        rest = chain([o for o in p["ops"] if group.get(o) == f"others:{scope}"])
        value = rest
        for b in reversed(conds):
            own = [o for o in p["ops"] if group.get(o) == b]
            links[(b, "then")] = chain(own) if own else scope_value(b)
            links[(b, "else")] = value
            value = b
        return value

    top = scope_value(None)
    tops = [b for b in p["branches"] if p["parent"].get(b) is None]
    last = tops[-1] if tops else None                         # the others keep their value: the branches' conditions
    p["keep_rows"] = [links[(b, "then")] and b for b in tops] if last and links.get((last, "else")) == "current" \
        else []                                               # choose the rows (a filter: a branch, its else unchanged)
    for a in p["after"]:                                      # roundings and limits: after every branch
        links[(a, "left")] = top
        top = a
    for g, b in zip(p["guards"], p["bounds"]):              # each limit its own bound, in written order
        links[(g, "right")] = b
    return links
