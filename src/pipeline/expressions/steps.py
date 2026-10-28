"""Operations, option B (2026-10-14): the same records as operations.py, where Jev decides the meaning. Code finds the
candidates (numbers, the column words and condition phrases from step 0); Jev answers closed questions about each
one in one call per request: what the number does, which named column it starts from, what each condition does with
its records. Code turns the answers into records (percent → factor, limits → guards and bounds). An answer below
MIN is abstained (an ambiguity record), never guessed.
"""
import re

from orbitrows.services.model.client import ask, choice
from orbitrows.pipeline.prompt import SURE
from .words import Record, column_mentions, nlp, occurrences, segment_of, segments

MIN = 0.5
CONTEXT = ("A user wrote one request that changes a number column of their shop database. Each number of the "
           "request is marked [n1: …]. The request may contain spelling mistakes: read it for its meaning.")
KIND = {   # what a number does that is not one operation of the catalog: a part of a limit, or nothing
    "upper_limit_computed": "This number computes a maximum from a column ('twice' in 'never above twice the cost').",
    "lower_limit_computed": "This number computes a minimum from a column ('10%' in 'never below the cost plus 10%').",
    "none": "Not a change of the value: part of a condition, a count, a name, or a repeated step already counted.",
}
FILLED_BY_NUMBER = ("number", "percentage", "value")         # the slot types a request's number fills


def number_options(words: str) -> dict:
    """The catalog's changes closest to a number's own words (use.search: changes of a number column, those with a
    slot a number fills); then what a number does that is no operation (KIND)."""
    from orbitrows.pipeline.operations import use
    c = use.catalog()["entries"]
    ids = [i for i in use.search(words, "value", "number", len(c))
           if any(x["type"] in FILLED_BY_NUMBER for x in use.slots(c[i]))][:use.K]
    return {i: f"{c[i]['name']}: {c[i]['behavior']} (e.g. '{c[i]['examples'][0]}')" for i in ids} | KIND


def plain_options(text: str) -> dict:
    """The catalog's changes of a number column with nothing to fill ("round them up", "make them positive"), closest to
    the request; then none."""
    from orbitrows.pipeline.operations import use
    c = use.catalog()["entries"]
    ids = [i for i in use.search(text, "value", "number", len(c)) if not use.slots(c[i])][:use.K]
    return {i: f"{c[i]['name']}: {c[i]['behavior']} (e.g. '{c[i]['examples'][0]}')" for i in ids} | {
        "no": "None of these."}


def slot_options(words: str, kind: str) -> dict:
    """What a written text (or, for a date column, a number) may fill: each slot of the catalog's changes the shared
    search finds for its words ("Replace a text: the text replaced" / "…: the text put in its place"); then none."""
    from orbitrows.pipeline.operations import use
    c, out = use.catalog()["entries"], {}
    fills = ("text", "value", "number") if kind == "date" else ("text", "value")
    for i in [i for i in use.search(words, "value", kind, len(c)) if any(
            x["type"] in fills for x in use.slots(c[i]))][:use.K]:
        for x in use.slots(c[i]):
            if x["type"] in fills:
                out[f"{i}.{x['name']}"] = f"{c[i]['name']}: {x['description']} (e.g. '{c[i]['examples'][0]}')"
    return out | {"none": TEXT_KIND["none"]}


def plain_text_options(text: str, kind: str) -> dict:
    """The catalog's changes of a text or date column with nothing to fill (capitals, lower case, trimmed, emptied)."""
    from orbitrows.pipeline.operations import use
    c = use.catalog()["entries"]
    ids = [i for i in use.search(text, "value", kind, len(c)) if not use.slots(c[i])][:use.K]
    return {i: f"{c[i]['name']}: {c[i]['behavior']} (e.g. '{c[i]['examples'][0]}')" for i in ids} | {
        "no": "None of these."}


def catalog_steps(cands, ans, add, start_of, column_of, ambiguities, stored, dated, target, text, names):
    """The text pass on the catalog (step 1.3): each written text (a number, for a date column) fills the slot its
    answer names; the slots of one change are grouped in text order ("replace X with Y": one step, X its old text, Y
    its new one); a slot still missing is taken from the change's previous step ("…, but with Orbit+ for the active
    ones": the replaced text said before), else asked; a value is checked as the old pass did (a date for a date
    column, a stored spelling). Then a change with nothing to fill (plain_text)."""
    from orbitrows.pipeline.operations import use
    c_ = use.catalog()["entries"]
    groups = []
    for c in cands:
        pick, kp = ans.get(f"{c['occ']}_kind", (None, {}))
        if pick is None or kp.get(pick, 0) < MIN:
            ambiguities.append({"problem": "what this text does: uncertain", "occurrence": c["occ"], "text": c["text"],
                                "start": c["start"], "end": c["end"], "answer": pick,
                                "probability": round(kp.get(pick, 0), 2)})
            continue
        if pick == "none" or "." not in pick:
            continue
        eid, slot = pick.split(".", 1)
        decl = next(x for x in use.slots(c_[eid]) if x["name"] == slot)
        fill = {}
        if decl["type"] == "value":                         # a new value: the old pass's checks
            v = c.get("iso") if c["kind"] == "date" else c["text"]
            if dated and c["kind"] != "date":
                ambiguities.append({"problem": f"'{c['text']}' is not a date the column can hold",
                                    "start": c["start"], "end": c["end"]})
                continue
            if c["kind"] == "date" and not v:
                ambiguities.append({"problem": f"date not read: '{c['text']}'", "start": c["start"], "end": c["end"]})
                continue
            same = next((x for x in stored if c["text"].casefold() == x.casefold()), None)
            if same:
                v = same
            elif stored and c["kind"] == "text" and not c.get("quoted"):
                sv, sp = ans.get(f"{c['occ']}_stored", ("as_written", {}))
                if sv != "as_written" and sp.get(sv, 0) >= MIN:
                    v = stored[int(sv[1:])]
            if stored and v not in stored:
                ambiguities.append({"problem": f"'{v}' is not one of the existing values ({', '.join(stored)})",
                                    "question": f"{c['occ']}_stored", "answer": v, "probability": 0.0,
                                    "status": "flagged", "start": c["start"], "end": c["end"]})
            fill[slot] = v
        elif decl["type"] == "number":                      # a length of time for a date: its unit
            unit, up = (c["unit"], {c["unit"]: 1}) if c.get("unit") else ans.get(f"{c['occ']}_unit", ("none", {}))
            if unit == "none" or up.get(unit, 0) < MIN:
                ambiguities.append({"problem": f"duration unit of '{c['text']}' unclear", "start": c["start"],
                                    "end": c["end"]})
                continue
            fill[slot] = c["value"] * (7 if unit == "weeks" else 1)
            if any(x["name"] == "unit" for x in use.slots(c_[eid])):
                fill["unit"] = "days" if unit == "weeks" else unit
        else:
            fill[slot] = c["text"]
        named = column_of(c)
        a, b = start_of(c), c["end"] + (1 if text[c["end"]:c["end"] + 1] in ("'", '"') else 0)
        g = next((g for g in reversed(groups) if g["entry"] == eid and slot not in g["fill"]), None)
        if g is None:
            groups.append(g := {"entry": eid, "fill": {}, "a": a, "b": b, "named": named})
        g["fill"] |= fill
        g["a"], g["b"], g["named"] = min(g["a"], a), max(g["b"], b), g["named"] or named
    for i, g in enumerate(groups):
        e = c_[g["entry"]]
        for x in use.slots(e):                              # a slot not written here: the change's previous step's
            if x["required"] and x["name"] not in g["fill"]:
                prev = next((h for h in reversed(groups[:i]) if h["entry"] == g["entry"] and x["name"] in h["fill"]),
                            None)
                if prev:
                    g["fill"][x["name"]] = prev["fill"][x["name"]]
        _, missing = use.fill(e, {"column": {"type": "column", "source": "t_", "reference": "c_"}} | g["fill"])
        if missing:
            ambiguities.append({"problem": f"'{text[g['a']:g['b']]}': {'; '.join(missing)}", "start": g["a"],
                                "end": g["b"]})
            continue
        left = ({"column": g["named"]} if g["named"] else {"unresolved": True}) if use.reads_column(e) else None
        add(entry_role(e), f"entry:{g['entry']}", left, {"fill": g["fill"], "source": text[g["a"]:g["b"]]},
            g["a"], g["b"], f"catalog.{g['entry']}")
    pt, pp = ans.get("plain_text", ("no", {}))
    e = plain_entry(pt) if pp.get(pt, 0) >= MIN else None
    if e is not None and (use.reads_column(e) or not groups):   # emptying, when no value is set: as the old pass
        t = next((c for c in names.values() if c["column"] == target), None)
        a, b = (t["start"], t["end"]) if t else (0, len(text))
        add(entry_role(e), f"entry:{pt}", ({"column": target} if t else {"unresolved": True})
            if use.reads_column(e) else None, None, a, b, f"catalog.{pt}")


def plain_entry(answer: str):
    from orbitrows.pipeline.operations import use
    return use.catalog()["entries"].get(answer) if answer not in (None, "no") else None


def entry_role(entry: dict) -> str:
    """Pass 2's role of a catalog change, from its builder: a cap guards, a rounding rounds, any other calculates."""
    if entry["builder_key"] == "clamp":
        return "guard"
    rounds = entry["builder_key"] in ("round_digits", "round_to_step") or (
        entry["builder_key"] == "function_of" and entry["config"].get("function") in ("ceil", "floor", "trunc"))
    return "rounding" if rounds else "calculation"


def slot_value(slot: dict, o: dict):
    """A number in the unit of the slot it fills: a percentage slot takes '10%' as 10 and 'a third' as 33.3…; a number
    slot takes '50%' as 0.5; any other number as written."""
    v = o["value"]
    if slot["type"] == "percentage":
        return v if o["percent"] else v * 100 if o["kind"] == "factor" and v < 1 else v
    return v / 100 if o["percent"] else v


KIND_RULES = [
    "Judge the marked number by the words around it, not by its position in the sentence.",
    "'plus 10%' on a column ('the cost plus 10%') is an increase of that column by 10%, not a share.",
    "A percentage after 'apply' or 'get' takes the direction the sentence gives it ('get 5%' after 'raise by 10%' is "
    "an increase).",
]
COLUMN_RULES = [
    "For a percentage OF a column ('10% of the cost'), answer the column it is taken of (the cost).",
    "Answer the column when the words of this step name it, wherever they stand in the step: 'Raise the price by 15%', "
    "'a 5% discount on the price', 'Deduct 11 from the price', 'quantity minus 1', 'twice their cost' all name one.",
    "Answer none when the step's own words name no column: 'then take 5 off', 'apply the 15%', 'halve it', or a formula "
    "given to some records instead of another ('but for black variants add 20 instead').",
]
USE = {
    "target": "It is the column the request changes, named as such ('Raise the price …').",
    "base": "It is the value a numbered step starts from or applies to ('by 15%', 'twice their cost', 'cost plus 10%').",
    "added": "Its value is added to the result, with no number of its own ('plus the reserved stock', 'add the cost').",
    "subtracted": "Its value is subtracted from the result, with no number of its own ('minus the cost').",
    "multiplied": "The result is multiplied by its value, with no number of its own ('the quantity times the unit "
                  "price', 'multiplied by the rate').",
    "divided": "The result is divided by its value, with no number of its own ('the price divided by the weight', "
               "'per unit of weight').",
    "set_to": "The value becomes this column's value ('set the cost to the price').",
    "upper_limit": "The result must never go above this column's value ('never above the cost').",
    "lower_limit": "The result must never go below this column's value ('never below the cost', 'keep the old price if "
                   "the new one is lower').",
    "other": "None of these.",
}
ROLE = {
    "where": "Only these records change; the request does not change the others.",
    "where_not": "These records must not change (they are left as they are).",
    "branch_condition": "These records get a different change than the other records.",
    "unresolved_condition": "The sentence does not say whether it limits the records or gives them a different change.",
}
ROLE_RULES = [
    "When the records are named right after a LATER step ('…, then halve it for active products', 'subtract 1 "
    "afterward at Rabat') and the sentence does not say whether they limit every step or only that last one, answer "
    "that the sentence does not say (unresolved).",
    "Named with the first step or before every step ('Raise the prices of the Orbit variants by 15%, then …', "
    "'For Orbit variants, cut …, then …'), they limit the whole request.",
]


def marked(text: str, occs: list[dict]) -> str:
    out, i = [], 0
    for o in occs:
        out += [text[i:o["start"]], f"[{o['occ']}: {text[o['start']:o['end']]}]"]
        i = o["end"]
    return "".join(out + [text[i:]])


async def extract(jev, text: str, upstream: dict, lang: str = "en") -> dict:
    if upstream.get("target_kind") in ("text", "date"):    # a text or date column: its own candidates and questions
        return await extract_textdate(jev, text, upstream, lang)
    return await extract_number(jev, text, upstream, lang)


async def extract_number(jev, text: str, upstream: dict, lang: str = "en") -> dict:
    """upstream: benchmark mode {target, columns: [words with their column], predicates: [text, sql]}; pipeline mode
    {target, words: [column words of step 3, column unknown], table_columns: {id: label}, predicates: [text, start,
    end], units: [{start, end, value}: a number converted to the target's unit]}: in pipeline mode each word's column
    is asked in the same call (the referencing is done here, decided 2026-10-15)."""
    preds = [p if "start" in p else p | {"start": (i := text.find(p["text"])), "end": i + len(p["text"])}
             for p in upstream.get("predicates", [])]
    occs = occurrences(text, lang, [(p["start"], p["end"]) for p in preds if p["start"] >= 0])
    for o in occs:                                          # "0.05 kg" on a column in grams: the value in its unit
        u = next((x for x in upstream.get("units", []) if x["start"] <= o["start"] < x["end"]), None)
        if u and not o["percent"] and u["value"] is not None:
            o["value"] = u["value"]
    piped = "words" in upstream
    cols = [dict(w, column=None) for w in upstream["words"]] if piped else column_mentions(text, upstream.get("columns", []))
    cols = [c for c in cols if not any(p["start"] <= c["start"] < p["end"] for p in preds)]
    names = {f"c{i}": c for i, c in enumerate(cols)}
    ctx = lambda c: text[max(0, c["start"] - 25):c["end"] + 15]
    col_opts = {k: (f"'{c['text']}' in '…{ctx(c)}…'" if piped else
                    f"'{c['text']}' (the column {c['column'].split('.c_')[-1]}) in '…{ctx(c)}…'") for k, c in names.items()}
    if piped:                                               # no word found by step 3: the column itself
        col_opts |= {f"t:{k}": f"The {v} (named in the step, though no word was marked)"
                     for k, v in upstream["table_columns"].items()}
        # the target stays an option: without it, "lower the price" (no word marked) was answered with the COST
        # (2026-10-15, A01/A28 built price = cost × 0.95): never remove the right answer from a closed question
    col_opts["none"] = "No column is named in this step's own words."
    state = {"request": marked(text, occs), "columns named in the request": col_opts}
    q = {}
    for o in occs:
        clause = text[slice(*segment_of(segments(text), o["start"]))].strip()
        q[f"{o['occ']}_kind"] = choice(CONTEXT, f"What does the number {o['occ']} ('{o['text']}') do?",
                                       number_options(clause),
                                       KIND_RULES + upstream.get("rules", []))
        q[f"{o['occ']}_column"] = choice(CONTEXT, f"The step with {o['occ']} ('{o['text']}', in '{clause}'): do its own "
                                                  f"words name the column it changes or starts from? Which one?",
                                         col_opts, COLUMN_RULES)
    for k, c in names.items():                              # column words: steps with no number ("plus the quantity")
        if piped:
            q[f"{k}_names"] = choice(CONTEXT, f"Which column does the word '{c['text']}' ('…{ctx(c)}…') name?",
                                     {**upstream["table_columns"], "none": "It does not name a column."})
        q[f"{k}_use"] = choice(CONTEXT, f"What does the request do with the column word '{c['text']}' "
                                        f"('…{text[max(0, c['start'] - 25):c['end'] + 15]}…')?",
                                        USE | ({"target": upstream["target_use"]} if upstream.get("target_use") else {}))
    for p in preds:
        if p["start"] >= 0:
            q[f"{p['id']}_role"] = choice(CONTEXT, f"What does the request do with the records '{p['text']}'?", ROLE,
                                         ROLE_RULES)
    q["plain_function"] = choice(CONTEXT, "Does the request change the value in one of these ways, without giving a "
                                          "number?", plain_options(text))
    q["round_plain"] = choice(CONTEXT, "Does the request round the value without giving a number of decimals or a "
                                       "multiple ('round the price', 'round everything')? If so, which named column?",
                              {**{k: f"Yes, it rounds {v}" for k, v in col_opts.items() if k != "none"},
                               "unnamed": "Yes, it rounds a result or 'it' / 'them', not a named column.",
                               "no": "No such rounding."})
    ans = await ask(jev, state, q)
    if piped:                                               # each word's column, from the same call
        for k, c in list(names.items()):
            pick, pp = ans.get(f"{k}_names", ("none", {}))
            if pick == "none" or pp.get(pick, 0) < MIN:
                names.pop(k)
            else:
                c["column"] = pick
                if pp.get(pick, 0) < SURE:
                    c["flag"] = round(pp.get(pick, 0), 2)          # kept, reported by the glue (formula.py)
        cols = list(names.values())

    doc = nlp(lang)(text)
    segs = segments(text)
    records, ambiguities = [], []

    def add(role, function, left, right, a, b, rule, occ=()):
        records.append(Record(role=role, function=function, left=left, right=right, text=text[a:b], start=a, end=b,
                              rule_id=rule, occurrences=list(occ)))

    def phrase_start(o):
        """The step's words start at the verb governing the number (parse), within its clause, never before the
        previous number (two steps of one clause keep their own words)."""
        seg = segment_of(segs, o["start"])
        prev = max([x["end"] for x in occs if x["end"] <= o["start"]] + [seg[0]])
        seg = (max(seg[0], prev), seg[1])
        tok = next((t for t in doc if t.idx >= o["start"]), None)
        while tok is not None and tok.head.i != tok.i and tok.pos_ not in ("VERB", "AUX"):
            tok = tok.head
        return tok.idx if tok is not None and seg[0] <= tok.idx < o["start"] else o["start"]

    for o in occs:
        kind, kp = ans.get(f"{o['occ']}_kind") or (None, {})
        col, cp = ans.get(f"{o['occ']}_column", ("none", {}))
        if kind is None or kp.get(kind, 0) < MIN:
            ambiguities.append({"problem": "what this number does: uncertain", "occurrence": o["occ"], "text": o["text"],
                                "start": o["start"], "end": o["end"], "answer": kind,
                                "probability": round(kp.get(kind, 0), 2), "step": "3 operation (Jev)"})
            continue
        if kind == "none":
            continue
        if piped:                                           # one column, several options (a word naming it, and the
            meant = {}                                      # column itself): their votes are added before the threshold
            for k, pr in cp.items():                        # (A34: 0.49 + 0.39 for the quantity was dropped as 0.49)
                key = names[k]["column"] if k in names else k[2:] if k.startswith("t:") else None
                if key:
                    meant[key] = meant.get(key, 0) + pr
            best = max(meant, key=meant.get) if meant else None
            if best and meant[best] >= MIN and meant[best] > cp.get("none", 0):
                word = max((k for k in cp if k in names and names[k]["column"] == best), key=cp.get, default=None)
                col, cp = (word, {word: meant[best]}) if word else (f"t:{best}", {f"t:{best}": meant[best]})
        c = names.get(col) if cp.get(col, 0) >= MIN else None
        if c is None and col.startswith("t:") and cp.get(col, 0) >= MIN:
            c = {"column": col[2:], "start": o["start"], "end": o["end"]}   # the column, without its words
            if col[2:] != upstream["target"] and kind in ("increase", "decrease", "set", "multiply", "divide"):
                # a change starting from ANOTHER column that no word names ("price = cost × 0.95"): shown, never silent
                ambiguities.append({"problem": f"the change starts from {col[2:]}, which no word names: to confirm",
                                    "occurrence": o["occ"], "answer": col, "probability": round(cp.get(col, 0), 2),
                                    "status": "flagged"})
        left = {"column": c["column"]} if c else {"unresolved": True}
        a, b = phrase_start(o), o["end"]
        if c:
            a, b = min(a, c["start"]), max(b, c["end"])
        if kind not in KIND:                                  # a catalog change: its own step, its slot filled
            from orbitrows.pipeline.operations import use
            e = use.catalog()["entries"][kind]
            slot = next(x for x in use.slots(e) if x["type"] in FILLED_BY_NUMBER)
            other = next((x for x in use.slots(e) if x["type"] == "column_reference"), None)
            right = {"value": slot_value(slot, o), "source": o["text"], "slot": slot["name"]}
            if other and c and c["column"] != upstream["target"]:   # "10% of the cost": the cost is its other column
                right["other"], left = {"column": c["column"]}, {"unresolved": True}
            add(entry_role(e), f"entry:{kind}", left if use.reads_column(e) else None, right, a, b,
                f"catalog.{kind}", [o["occ"]])
            continue
        v, p, rule = o["value"], o["percent"], f"jev.{kind}"
        pct = lambda f: {"value": round(f, 6), "source": o["text"]}
        if kind in ("upper_limit_computed", "lower_limit_computed"):   # a limit computed from a column: bound + guard
            fn = "clamp_max" if kind == "upper_limit_computed" else "clamp_min"
            # a percentage or a factor word multiplies the column ("plus 10%" → × 1.1, "twice" → × 2); a plain
            # number is added to it ("the cost plus 10" → + 10)
            op, f = ("*", (1 + v / 100) if p else v) if p or o["kind"] in ("factor", "factor_verb") else ("+", v)
            add("bound", op, left, pct(f), c["start"] if c and c["start"] < o["start"] else o["start"], b, rule, [o["occ"]])
            add("guard", fn, {"unresolved": True}, {"unresolved": True}, a, o["start"], rule + ".guard")

    for k, c in names.items():                              # steps with no number, on a column word
        use, up = ans.get(f"{k}_use", ("other", {}))
        if up.get(use, 0) < MIN or use in ("target", "base", "other"):
            continue
        if any(c["start"] < r.end and r.start < c["end"] for r in records):
            continue                                            # already a numbered step's operand
        if use == "set_to" and c["column"] == upstream["target"]:
            continue                                            # the target is what changes, not a value to copy
        seg = segment_of(segs, c["start"])
        if use in ("upper_limit", "lower_limit") and any(r.role in ("guard", "bound") and seg[0] <= r.start < seg[1]
                                                          for r in records):
            continue                                            # the clause's limit is already written
        col = {"column": c["column"]}
        if use in ("added", "subtracted", "multiplied", "divided"):
            m = re.search(r"(\w+\s+){0,2}$", text[:c["start"]])
            add("calculation", {"added": "+", "subtracted": "-", "multiplied": "*", "divided": "/"}[use],
                {"unresolved": True}, col, m.start() if m else c["start"], c["end"], f"jev.column_{use}")
        elif use == "set_to":
            add("calculation", "set", None, col, c["start"], c["end"], "jev.column_set_to")
        else:
            add("guard", "clamp_max" if use == "upper_limit" else "clamp_min", {"unresolved": True}, col, c["start"],
                c["end"], f"jev.column_{use}")
    rp, rpp = ans.get("round_plain", ("no", {}))
    pf, pfp = ans.get("plain_function", ("no", {}))
    plain = plain_entry(pf) if pfp.get(pf, 0) >= MIN else None   # the catalog's change with nothing to fill
    rounds = plain is not None and entry_role(plain) == "rounding"
    if rp != "no" and rpp.get(rp, 0) >= MIN and not any(r.role == "rounding" for r in records) and not rounds:
        # "round them up": the plain function's rounding, never a second one
        m = re.search(r"\bround", text, re.I)
        c = names.get(rp)
        a = m.start() if m else (c["start"] if c else 0)
        add("rounding", "round", {"column": c["column"]} if c else {"unresolved": True}, None, a,
            c["end"] if c and c["end"] > a else a + 5, "jev.round_plain")
    if plain is not None:                                     # a change with nothing to fill: on the value the other
        t = next((c for c in cols if c["column"] == upstream["target"]), None)   # steps give (pass 2 links it)
        a, b = (t["start"], t["end"]) if t else (0, len(text))
        add(entry_role(plain), f"entry:{pf}", {"unresolved": True}, None, a, b, f"catalog.{pf}")

    for p in preds:
        if p["start"] < 0:
            continue
        role, rp_ = ans.get(f"{p['id']}_role", (None, {}))
        if role is None or rp_.get(role, 0) < MIN:
            role = "unresolved_condition"
        add(role, None, {"pred": p["id"]}, None, p["start"], p["end"], f"jev.{role}")
        if role == "branch_condition":
            add("branch", "case", {"pred": p["id"]}, None, p["start"], p["end"], "branch.case")

    t = next((c for c in cols if c["column"] == upstream["target"]), None)
    if t:
        tok = next((x for x in doc if x.idx >= t["start"]), None)
        while tok is not None and tok.head.i != tok.i and tok.pos_ not in ("VERB", "AUX"):
            tok = tok.head
        a = tok.idx if tok is not None and tok.idx < t["start"] else t["start"]
        add("assignment", "assign", {"column": upstream["target"]}, {"unresolved": True}, a, t["end"], "parse.target")
    for k, v in ans.items():                                # kept but shown to the user, as in the pipeline
        if isinstance(v, (list, tuple)) and MIN <= v[1].get(v[0], 0) < SURE and v[0] not in ("none", "no", "other"):
            ambiguities.append({"problem": "answer kept but uncertain", "question": k, "answer": v[0],
                                "probability": round(v[1].get(v[0], 0), 2), "status": "flagged"})
    records.sort(key=lambda r: r.start)
    return {"records": [r.model_dump() for r in records], "ambiguities": ambiguities, "answers": ans,
            "occurrences": occs}


# --- text and date changes (basic version, 2026-10-16; relative dates, joins, case changes: notice.md #17–#19) -------

TEXT_CONTEXT = ("A user wrote one request that changes a text or date column of their shop database. The texts, "
                "numbers and dates of the request that may be part of the change are marked [x1: …].")
TEXT_KIND = {   # what a text or a date does that is not one operation of the catalog
    "none": "Not a change of the column: part of a condition, a name used to choose the records, or a count.",
}
UNIT = {"days": "days", "weeks": "weeks", "months": "months", "years": "years", "none": "Not a duration."}
CASE_RULES = ["A text written or replaced that happens to be in capitals is not a case change: 'replace Orbit with "
              "ORBIT', 'add ' NEW' at the end' change only those words: answer none.",
              "A case change is asked for the column's whole text: 'write the brands in capitals', 'put the colours in "
              "lower case', 'capitalise the names'."]
TEXT_USE = {"target": "It is the column the request changes.",
            "set_to": "The changed column becomes this column's value ('set the barcode to the SKU').",
            "other": "None of these."}


def iso(date_text: str) -> str | None:
    """A written date as YYYY-MM-DD, or None: datephrase (units/datephrase, over Duckling). None also when the
    reader would ask the user (01/02/2026: January 2 or February 1?): its questions are not forwarded yet; or when its
    service is down. Missing parts and relative dates are filled from the session's date (units/dates.today); a time
    of day is dropped."""
    from orbitrows.units import dates
    from orbitrows.units.datephrase import EngineUnavailable, TemporalContext, read_temporal
    try:
        r = read_temporal(date_text, context=TemporalContext(reference_date=dates.today()))   # the session's date
    except EngineUnavailable:                               # the service is down: not read (the formula asks)
        return None
    return r.date.isoformat() if r.date else None


def text_candidates(text, upstream, preds, dated):
    """The texts the request may write (step 3's written values / the benchmark's texts, and quoted texts), its dates
    (upstream "dates": units/dates, read by step 6) and, for a date column, its lengths of time (their unit read) and
    other numbers. Positions in the text."""
    inside = lambda a: any(p["start"] <= a < p["end"] for p in preds if p["start"] >= 0)
    out = []
    for m in re.finditer(r"'([^']*)'|\"([^\"]*)\"", text):                 # quoted: the text between the quotes
        g = 1 if m.group(1) is not None else 2
        out.append({"text": m.group(g), "start": m.start(g), "end": m.end(g), "kind": "text", "quoted": True})
    for t in upstream.get("texts", []):
        a = t.get("start", text.find(t["text"]))
        if a >= 0 and not any(c["start"] <= a < c["end"] for c in out):
            out.append({"text": t["text"], "start": a, "end": a + len(t["text"]), "kind": "text"})
    for d in upstream.get("dates", []):
        v, a = d["value"], d["start"]
        if inside(a) or any(c["start"] <= a < c["end"] for c in out):
            continue
        if v["kind"] == "date":
            out.append({"text": d["text"], "start": a, "end": d["end"], "kind": "date", "iso": v["date"]})
        elif v["kind"] == "duration" and dated:             # "2 weeks later": its quantity, its unit known
            out.append({"text": d["text"], "start": a, "end": d["end"], "kind": "number", "value": v["quantity"],
                        "unit": v["unit"]})
        skip = [(c["start"], c["end"]) for c in out] + [(p["start"], p["end"]) for p in preds if p["start"] >= 0]
        for o in occurrences(text, "en", skip):
            if not o["percent"]:
                out.append({"text": o["text"], "start": o["start"], "end": o["end"], "kind": "number", "value": o["value"]})
    # a quoted text is the user's own text, even where step 6 also made a condition of it ("replace 'cotton'")
    out = [c for c in out if c.get("quoted") or not inside(c["start"])]
    if dated:                                               # a date column: a written value that is a date is a date
        for c in out:
            if c["kind"] == "text" and iso(c["text"]):
                c.update(kind="date", iso=iso(c["text"]))
    out.sort(key=lambda c: c["start"])
    for i, c in enumerate(out, 1):
        c["occ"] = f"x{i}"
    return out


async def extract_textdate(jev, text: str, upstream: dict, lang: str = "en") -> dict:
    dated = upstream["target_kind"] == "date"
    preds = [p if "start" in p else p | {"start": (i := text.find(p["text"])), "end": i + len(p["text"])}
             for p in upstream.get("predicates", [])]
    cands = text_candidates(text, upstream, preds, dated)
    piped = "words" in upstream
    cols = [dict(w, column=None) for w in upstream["words"]] if piped else column_mentions(text, upstream.get("columns", []))
    cols = [c for c in cols if not any(p["start"] <= c["start"] < p["end"] for p in preds)
            and not any(x["start"] <= c["start"] < x["end"] for x in cands)]
    names = {f"c{i}": c for i, c in enumerate(cols)}
    ctx = lambda c: text[max(0, c["start"] - 25):c["end"] + 15]
    col_opts = {k: f"'{c['text']}' in '…{ctx(c)}…'" for k, c in names.items()}
    if piped:
        col_opts |= {f"t:{k}": f"The {v} (named, though no word was marked)" for k, v in upstream["table_columns"].items()}
    col_opts["none"] = "No column is named in this step's own words."
    q = {}
    stored = upstream.get("stored_values") or []           # a short list of existing values (a status): A02
    tkind = "date" if dated else "text"
    for c in cands:
        clause = text[slice(*segment_of(segments(text), c["start"]))].strip()
        q[f"{c['occ']}_kind"] = choice(TEXT_CONTEXT, f"What does {c['occ']} ('{c['text']}') do?",
                                       slot_options(clause, tkind))
        exact = any(c["text"].casefold() == v.casefold() for v in stored)
        if stored and c["kind"] == "text" and not c.get("quoted") and not exact:   # "blue" IS a stored value: no question
            q[f"{c['occ']}_stored"] = choice(TEXT_CONTEXT, f"If {c['occ']} ('{c['text']}') gives the column's new value, "
                                                           f"which of its existing values does it mean?",
                                             {**{f"s{i}": f"'{v}'" for i, v in enumerate(stored)},
                                              "as_written": f"A value the column has never held, to be written letter for "
                                                            f"letter as '{c['text']}'."},
                                             ["A value can be named by another FORM OF THE SAME WORD: a verb ('Archive "
                                              "the products': archived; 'activate them': active), a plural or another "
                                              "spelling. Never another word with a close meaning: blue is not navy, "
                                              "crimson is not red.",
                                              "Choose the letter-for-letter option only when the request clearly brings "
                                              "a new value."])
        q[f"{c['occ']}_column"] = choice(TEXT_CONTEXT, f"The step with {c['occ']} ('{c['text']}'): do its own words "
                                                       f"name the column it changes? Which one?", col_opts, COLUMN_RULES)
        if c["kind"] == "number" and not c.get("unit"):
            q[f"{c['occ']}_unit"] = choice(TEXT_CONTEXT, f"{c['occ']} ('{c['text']}') counts what?", UNIT)
    for k, c in names.items():
        if piped:
            q[f"{k}_names"] = choice(TEXT_CONTEXT, f"Which column does the word '{c['text']}' ('…{ctx(c)}…') name?",
                                     {**upstream["table_columns"], "none": "It does not name a column."})
        q[f"{k}_use"] = choice(TEXT_CONTEXT, f"What does the request do with the column word '{c['text']}' "
                                             f"('…{ctx(c)}…')?",
                                             TEXT_USE | ({"target": upstream["target_use"]} if upstream.get("target_use")
                                                         else {}))
    for p in preds:
        if p["start"] >= 0:
            q[f"{p['id']}_role"] = choice(TEXT_CONTEXT, f"What does the request do with the records '{p['text']}'?",
                                         ROLE, ROLE_RULES)
    q["plain_text"] = choice(TEXT_CONTEXT, "Does the request change the column's whole value in one of these ways, "
                                           "with nothing written to fill?", plain_text_options(text, tkind), CASE_RULES)
    marked_text = marked(text, [{"start": c["start"], "end": c["end"], "occ": c["occ"]} for c in cands])
    ans = await ask(jev, {"request": marked_text, "columns named in the request": col_opts}, q)
    if piped:
        for k, c in list(names.items()):
            pick, pp = ans.get(f"{k}_names", ("none", {}))
            if pick == "none" or pp.get(pick, 0) < MIN:
                names.pop(k)
            else:
                c["column"] = pick

    doc = nlp(lang)(text)
    segs = segments(text)
    records, ambiguities = [], []
    target = upstream["target"]

    def add(role, function, left, right, a, b, rule):
        records.append(Record(role=role, function=function, left=left, right=right, text=text[a:b], start=a, end=b,
                              rule_id=rule))

    def start_of(c):                                        # the step's words start at its verb (parse), in its clause
        seg = segment_of(segs, c["start"])
        prev = max([x["end"] for x in cands if x["end"] <= c["start"]] + [seg[0]])
        tok = next((t for t in doc if t.idx >= c["start"]), None)
        while tok is not None and tok.head.i != tok.i and tok.pos_ not in ("VERB", "AUX"):
            tok = tok.head
        return tok.idx if tok is not None and max(seg[0], prev) <= tok.idx < c["start"] else c["start"]

    def column_of(c):
        col, cp = ans.get(f"{c['occ']}_column", ("none", {}))
        meant = {}
        for k, pr in cp.items():                            # options naming the same column: their votes added (A34)
            key = names[k]["column"] if k in names else k[2:] if k.startswith("t:") else None
            if key:
                meant[key] = meant.get(key, 0) + pr
        best = max(meant, key=meant.get) if meant else None
        return best if best and meant[best] >= MIN and meant[best] > cp.get("none", 0) else None

    catalog_steps(cands, ans, add, start_of, column_of, ambiguities, stored, dated, target, text, names)
    for k, c in names.items():                              # copy another column
        use, up = ans.get(f"{k}_use", ("other", {}))
        if use == "set_to" and up.get(use, 0) >= MIN and c["column"] != target:
            add("calculation", "set", None, {"column": c["column"]}, c["start"], c["end"], "jev.column_set_to")
    for p in preds:
        if p["start"] < 0:
            continue
        role, rp_ = ans.get(f"{p['id']}_role", (None, {}))
        if role is None or rp_.get(role, 0) < MIN:
            role = "unresolved_condition"
        add(role, None, {"pred": p["id"]}, None, p["start"], p["end"], f"jev.{role}")
        if role == "branch_condition":
            add("branch", "case", {"pred": p["id"]}, None, p["start"], p["end"], "branch.case")
    t = next((c for c in names.values() if c["column"] == target), None)
    if t:
        tok = next((x for x in doc if x.idx >= t["start"]), None)
        while tok is not None and tok.head.i != tok.i and tok.pos_ not in ("VERB", "AUX"):
            tok = tok.head
        add("assignment", "assign", {"column": target}, {"unresolved": True},
            tok.idx if tok is not None and tok.idx < t["start"] else t["start"], t["end"], "parse.target")
    for k, v in ans.items():
        if isinstance(v, (list, tuple)) and MIN <= v[1].get(v[0], 0) < SURE and v[0] not in ("none", "no", "other"):
            ambiguities.append({"problem": "answer kept but uncertain", "question": k, "answer": v[0],
                                "probability": round(v[1].get(v[0], 0), 2), "status": "flagged"})
    records.sort(key=lambda r: r.start)
    return {"records": [r.model_dump() for r in records], "ambiguities": ambiguities, "answers": ans,
            "occurrences": cands}
