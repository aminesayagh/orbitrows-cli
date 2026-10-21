"""Operations, level 1 (2026-10-14): the same records as operations.py, without hand-written word rules. What a
number does is typed by GLiNER2 zero-shot (span labels with descriptions, in context); which column it applies to and
which clause a condition belongs to come from spaCy's dependency parse (a learned model). Regex only reads digits and
offsets. A number the model does not type is abstained, never guessed.

Known limits (measured in v2/bench/operations, see run_model.py): zero-shot condition classification was unusable
(8/18 on the tuning set, confident and wrong), so a condition's role comes from the structure: does its clause own a
change of its own, and is there another change outside it.
"""
import re

from orbitrows.services.extractor import client as extractor
from .operations import Record, column_mentions, nlp, occurrences

AMOUNT = {
    "increase amount": "a number by which a value is raised or increased",
    "decrease amount": "a number by which a value is lowered, reduced or taken off",
    "new value": "a number that a value is set to or replaced by",
    "multiplier": "a factor that a value is multiplied by, like twice, half, a third",
    "divisor": "a number that a value is divided by",
    "upper limit": "a maximum the value must never go above",
    "lower limit": "a minimum the value must never go below",
    "rounding step": "the multiple a value is rounded to, like the nearest 5",
    "decimal places": "the number of decimals a value is rounded to",
}
KEEP = {"excluded": "the records in brackets are left unchanged, not changed",
        "changed": "the records in brackets are changed"}
MIN = 0.3                                                   # below: the number is abstained


def tokens_in(doc, a, b):
    return [t for t in doc if a <= t.idx < b]


def governing_verb(tok):
    while tok.head.i != tok.i and tok.pos_ not in ("VERB", "AUX"):   # spaCy makes a new object per access: compare positions
        tok = tok.head
    return tok


def column_of(doc, occ, cols):
    """The column named in the number's own phrase: a column word that is the number's head noun ("twice their cost"),
    or a direct dependent of the verb governing the number (its object or a prepositional object), by the parse."""
    toks = tokens_in(doc, occ["start"], occ["end"])
    if not toks:
        return None
    t = toks[0]
    head = t
    while head.head.i != head.i and head.pos_ not in ("VERB", "AUX", "NOUN", "PROPN"):
        head = head.head
    near = {head.i} if head.pos_ in ("NOUN", "PROPN") else set()
    v = governing_verb(t)
    for c in v.children:
        if c.dep_ in ("dobj", "obj", "nsubjpass", "attr", "oprd"):
            near.add(c.i)
        if c.dep_ in ("prep", "prt", "dative"):
            near |= {g.i for g in c.children if g.dep_ in ("pobj", "obj")}
    for c in cols:
        if any(doc[i].idx < c["end"] and c["start"] < doc[i].idx + len(doc[i]) for i in near):
            return c
    return None


def extract(text: str, upstream: dict, lang: str = "en") -> dict:
    model = extractor.connect()
    doc = nlp(lang)(text)
    preds = [p | {"start": (i := text.find(p["text"])), "end": i + len(p["text"])} for p in upstream.get("predicates", [])]
    occs = occurrences(text, lang, [(p["start"], p["end"]) for p in preds if p["start"] >= 0])
    cols = [c for c in column_mentions(text, upstream.get("columns", []))
            if not any(p["start"] <= c["start"] < p["end"] for p in preds)]
    ents = model.extract_entities(text, AMOUNT, threshold=MIN, include_confidence=True, include_spans=True)["entities"]
    spans = [(lab, x["start"], x["end"], x["confidence"]) for lab, xs in ents.items() for x in xs]
    records, ambiguities = [], []

    def add(role, function, left, right, a, b, rule, occ=()):
        records.append(Record(role=role, function=function, left=left, right=right, text=text[a:b], start=a, end=b,
                              rule_id=rule, occurrences=list(occ)))

    limits = [s for s in spans if s[0] in ("upper limit", "lower limit")]
    for o in occs:
        over = [s for s in spans if s[1] < o["end"] and o["start"] < s[2]]
        if not over:
            ambiguities.append({"problem": "the model did not type this number", "occurrence": o["occ"],
                                "text": o["text"], "start": o["start"], "end": o["end"], "step": "3 operation (model)"})
            continue
        lab, a, b, conf = max(over, key=lambda s: s[3])
        c = column_of(doc, o, cols)
        left = {"column": c["column"]} if c else {"unresolved": True}
        val = {"value": o["value"], "source": o["text"]}
        a, b = min(a, o["start"]), max(b, o["end"])
        inside = [s for s in limits if s[1] <= o["start"] and o["end"] <= s[2] and s[0] != lab]
        role = "bound" if inside or (lab in ("upper limit", "lower limit") and c) else "calculation"
        rule = f"model.{lab.replace(' ', '_')}"
        p = o["percent"]
        if lab == "increase amount":
            add(role, "*" if p else "+", left, {"value": round(1 + o["value"] / 100, 6), "source": o["text"]} if p else val,
                a, b, rule, [o["occ"]])
        elif lab == "decrease amount":
            add(role, "*" if p else "-", left, {"value": round(1 - o["value"] / 100, 6), "source": o["text"]} if p else val,
                a, b, rule, [o["occ"]])
        elif lab == "new value":
            add(role, "set", None, val, a, b, rule, [o["occ"]])
        elif lab == "multiplier":
            add(role, "*", left, {"value": round(o["value"] / 100, 6) if p else o["value"], "source": o["text"]}, a, b,
                rule, [o["occ"]])
        elif lab == "divisor":
            add(role, "/", left, val, a, b, rule, [o["occ"]])
        elif lab in ("upper limit", "lower limit"):
            fn = "clamp_max" if lab == "upper limit" else "clamp_min"
            if c:                                           # a limit computed from a column: "twice their cost"
                add("bound", "*", left, val, a, b, rule, [o["occ"]])
                add("guard", fn, {"unresolved": True}, {"unresolved": True}, a, b, rule + ".computed")
            else:
                add("guard", fn, {"unresolved": True}, val, a, b, rule, [o["occ"]])
        elif lab == "rounding step":
            add("rounding", "round_to", left, val, a, b, rule, [o["occ"]])
        elif lab == "decimal places":
            add("rounding", "round", left, val, a, b, rule, [o["occ"]])
        for s in inside:                                    # the guard a bound belongs to
            if not any(r.role == "guard" and r.start == s[1] for r in records):
                add("guard", "clamp_max" if s[0] == "upper limit" else "clamp_min", {"unresolved": True},
                    {"unresolved": True}, s[1], s[2], f"model.{s[0].replace(' ', '_')}.computed")

    # condition roles, from the structure: the clause (the condition's governing verb in the parse) owns a change of
    # its own and another change exists outside it → a branch; the model says "left unchanged" → where_not
    actions = [r for r in records if r.role in ("calculation", "rounding")]
    for p in preds:
        if p["start"] < 0:
            ambiguities.append({"problem": "condition phrase not found", "pred": p["id"], "step": "0 upstream"})
            continue
        toks = tokens_in(doc, p["start"], p["end"])
        v = governing_verb(toks[0]) if toks else None
        own = [r for r in actions if v is not None and any(governing_verb(t) is v for t in tokens_in(doc, r.start, r.end))]
        outside = [r for r in actions if r not in own]
        marked = text[:p["start"]] + "[" + p["text"] + "]" + text[p["end"]:]
        keep = model.classify_text(marked, {"role": KEEP}, threshold=0.0, include_confidence=True)["role"]
        if keep["label"] == "excluded" and keep["confidence"] >= 0.5 and not own:
            role, rule = "where_not", "model.excluded"
        elif own and outside:
            role, rule = "branch_condition", "parse.own_change"
        else:
            role, rule = "where", "parse.no_own_change"
        add(role, None, {"pred": p["id"]}, None, p["start"], p["end"], rule)
        if role == "branch_condition":
            add("branch", "case", {"pred": p["id"]}, None, p["start"], p["end"], "branch.case")

    # the assignment: the target column's first mention and the verb governing it (parse)
    t = next((c for c in cols if c["column"] == upstream["target"]), None)
    if t:
        toks = tokens_in(doc, t["start"], t["end"])
        v = governing_verb(toks[0]) if toks else None
        a = v.idx if v is not None and v.idx < t["start"] else t["start"]
        add("assignment", "assign", {"column": upstream["target"]}, {"unresolved": True}, a, t["end"], "parse.target")
    records.sort(key=lambda r: r.start)
    prov = {o["occ"]: [i for i, r in enumerate(records) if o["occ"] in r.occurrences] for o in occs}
    return {"records": [r.model_dump() for r in records], "ambiguities": ambiguities, "provenance": prov,
            "occurrences": occs}
