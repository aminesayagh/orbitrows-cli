"""Step 3 of prompt → AST: the values each request names, and where they are stored (decided 2026-10-10; since
2026-10-12 the phrases come from the value linker, linker.py, and GLiNER2 together: see run()).

    GLiNER2  proposes value phrases (recall: three labels, one call each; overlapping phrases kept once, the longest);
             a phrase without a letter (10%, 170) is a number for later steps, never searched
    Jev      call 1, per phrase: its role (lookup: finds existing records · written: a new value the request stores
             · number · not a value), where the prompt says it is stored (a column, a kind of record, not said), and
             whether a word of the prompt names that column (a column only guessed from the value is searched)
    SQLite   only for a lookup the prompt does not tie to a column: the distinct values of the text columns (of the
             named table, else of every table) close to the phrase (rapidfuzz): exact (ignoring case) > typo >
             contains (the phrase inside a stored value); the best kind found is kept; last, over every table, a stored
             value written whole inside the phrase (within: "Stationery products"), the longest one. A phrase found nothing: its
             shorter parts are searched; when only a part matched, the words around it are searched too (exact or
             typo only) and become a second value ("PaperHub stationery": PaperHub, Stationery). Nothing in the
             named table: every table is searched and the answer flagged (the user confirms; never a silent
             override of what they wrote)
    Jev      call 2, only when several columns match: which one, or none (a new or unknown value)

The user's column always wins: "whose brand is Casa Living" is brand = 'Casa Living' even if Casa Living is a
supplier, and is not searched (0 rows is SQL's honest answer). A written value is never searched ("add the supplier
Nordic Paper" must not become PaperHub), and existence conditions ("if it does not exist") are SQL's, later.

Section "values" of cases/<id>.json: {"values": [{"id", "request", "text", "start", "end", "role", "column"?
("t_x.c_y"), "match"? (given · exact · typo · contains · within · none), "stored"? [values found], "flagged"?}], "warnings",
"jev"}. Examples in the descriptions and rules must not be test prompts (cases/).
"""
import re

from rapidfuzz import fuzz

from . import SURE
from .asking import question
from orbitrows.pipeline.store.profile import listed
from orbitrows.services.model.client import ask, choice, yes_no

LABELS = {                                                    # one call each (split.py: long label sets return nothing)
    "name": "a proper name: a brand, a company, a product, a place, a category, a person",
    # "item" finds names made of ordinary words ("Desk Lamp"), sometimes with the record word ("… products"): the
    # shorter overlapping phrases of the other labels are kept as parts and searched when the whole finds nothing
    "item": "the name of a specific product or item, even when made of ordinary words, like 'Travel Kettle' or "
            "'Kids Raincoat'",
    "code": "a code or identifier like a SKU, a reference or a barcode",
    "attribute": "a word describing records by a stored property, like a color, a size, a status or a country",
    "new value": "the value a request sets, gives or renames to, usually after 'to', 'as' or 'called'",
}
THRESHOLD = 0.3
TYPO = 85                                                     # rapidfuzz ratio for a spelling mistake
SEMANTIC_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"   # v1's row matching (poc.py)
SEMANTIC_MIN = 0.3                                            # v1's SEMANTIC_MIN: below, no candidate at all
SEMANTIC_TOP = 3                                              # the right value was always in the top 3 when tested
SCOPE_MIN = 0.8                                               # limits SQLite's search to one table: kept at 0.8, the
                                                              # 0.7 measurement (prompt_ast/__init__.py) did not cover it

CONTEXT = ("A user wrote a prompt to edit or read their store database (tables and columns in `tables`). The "
           "prompt was split into the requests in `requests`. The phrases in `phrases` were found in it as possible "
           "values.")
ROLES = {
    "lookup": "It finds existing records: the request uses it to choose the rows it reads or changes.",
    "written": "A new value the request stores: a new name, a new status, the values of a new record.",
    "number": "A quantity, price, percentage, date or duration, to compare or compute with.",
    "not_value": "Not a value: a column, a table, a kind of record, an ordinary word, or a state measured by a number "
                 "('sold out', 'expensive': a condition on a quantity or a price, not a stored word).",
}
ROLE_RULES = [
    "`stored_in`: the store holds this phrase (or a close spelling or meaning) in these columns: it is a lookup, "
    "unless the request writes it as a new value; a partial or meaning match may still be a coincidence.",
    "'rename the Lighting category to Lamps': Lighting is lookup (it finds the category), Lamps is written.",
    "A value in a condition is lookup even if it may not exist in the store; a value in 'add … if it does not "
    "exist' is written: the database checks the existence, not this answer.",
]
WRITTEN_RULES = [
    "Answer yes only when a word of the prompt names the column for this value: 'whose brand is Kora' (brand), "
    "'in the Fes warehouse' (warehouse), 'the color red' (color).",
    "Answer no when the column is only guessed from the value itself: 'the Kora lamp', 'the red ones', 'the "
    "pending orders' name no column.",
]
OWN_RULES = [
    "Answer yes when the value is the name of one record of the kind the prompt says: 'the supplier Kora', 'the "
    "Kora supplier' (a supplier named Kora), 'the Lumo lamp' (a product named Lumo lamp).",
    "Answer no when the value describes other records by something linked to them: 'the Kora products' (products "
    "linked to Kora), 'Kora items', 'the red shirts'.",
]
NAMED_RULES = [
    "`stored_in` says where the store holds the value; when a word of the prompt names another column ('whose brand "
    "is Kora' while Kora is stored as a supplier), the user's column wins.",
    "Choose a column only when the prompt names it for this value ('whose brand is Kora': the brand column), even "
    "if the value looks like it belongs elsewhere.",
    "Choose a kind of record when the value is that record's own name ('the supplier Kora': a suppliers record). A "
    "value describing other records ('the Kora products', 'products from Kora') does not name its column: not said.",
    "Choose not said when nothing ties the value to a column or a record kind.",
]


def text_columns(schema: list[dict], table: str | None = None) -> list[tuple]:
    return [(t["definition"]["table_id"], t["definition"]["sql_name"], c["column_id"], c["sql_name"])
            for t in schema if table in (None, t["definition"]["table_id"])
            for c in t["definition"]["columns"] if c["sql_type"] == "TEXT"]


def phrases(prompt: str, requests: list[dict], model) -> list[dict]:
    """GLiNER2's value phrases, overlapping ones kept once (the longest), each in the request containing it."""
    spans = []
    for label, description in LABELS.items():
        r = model.extract_entities(prompt, {label: {"description": description, "threshold": THRESHOLD}},
                                   include_confidence=True, include_spans=True, overlap_policy="flat")
        spans += r.get("entities", {}).get(label, [])
    spans = [s for s in spans if re.search(r"[^\W\d_]", s["text"])]
    for s in spans:                                         # a quote or a comma at the edge is not part of the value
        t = s["text"]                                       # ("cotton'" → "cotton", A44, 2026-10-16)
        lead, trail = len(t) - len(t.lstrip("'\"‘’“”,;:. ")), len(t) - len(t.rstrip("'\"‘’“”,;:. "))
        if lead or trail:
            s.update(text=t[lead:len(t) - trail], start=s["start"] + lead, end=s["end"] - trail)
    spans = [s for s in spans if s["text"]]
    kept = []
    for s in sorted(spans, key=lambda s: (-(s["end"] - s["start"]), -s["confidence"])):
        if all(s["end"] <= k["start"] or s["start"] >= k["end"] for k in kept):
            kept.append(s)
    out = []
    for i, s in enumerate(sorted(kept, key=lambda s: s["start"]), 1):
        req = next((r["id"] for r in requests if r["start"] <= s["start"] and s["end"] <= r["end"]), None)
        parts = sorted({x["text"] for x in spans if s["start"] <= x["start"] and x["end"] <= s["end"]
                        and x["text"] != s["text"]}, key=len, reverse=True)
        out.append({"id": f"v{i}", "request": req, "text": s["text"], "start": s["start"], "end": s["end"],
                    **({"parts": parts} if parts else {})})
    return out


def search(db, schema: list[dict], text: str, table: str | None = None,
           kinds=("exact", "typo", "contains")) -> list[dict]:
    """Columns holding values close to `text`: [{"column": "t_x.c_y", "match", "stored": [...]}], best kind only."""
    want, found = text.casefold(), {"exact": [], "typo": [], "contains": []}
    for tid, tname, cid, cname in text_columns(schema, table):
        hits = {"exact": [], "typo": [], "contains": []}
        for v in listed(db, tname, cname):                   # the profile's values: the rows are never read
            have = str(v).casefold()
            if have == want:
                hits["exact"].append(v)
            elif fuzz.ratio(have, want) >= TYPO:
                hits["typo"].append(v)
            elif len(want) >= 3 and re.search(rf"\b{re.escape(want)}", have):
                hits["contains"].append(v)
        for kind, values in hits.items():
            if values:
                found[kind].append({"column": f"{tid}.{cid}", "match": kind, "stored": sorted(values)})
    return next((found[k] for k in kinds if found[k]), [])


_ENCODER, _VECTORS = None, {}


def encoder():
    """The meaning model, loaded once per process (the linker's too: one copy)."""
    global _ENCODER
    if _ENCODER is None:
        from sentence_transformers import SentenceTransformer
        _ENCODER = SentenceTransformer(SEMANTIC_MODEL)
    return _ENCODER


def meaning(db, schema: list[dict], text: str, near: str | None = None) -> list[dict]:
    """Last tier, when no spelling matches: stored values close in meaning, in any language ("rouges" → red,
    "vêtements" → Clothing), by multilingual embeddings over the columns with few distinct values; the phrase and
    each of its words are compared. The SEMANTIC_TOP closest, each a candidate Jev confirms or refuses (call 2):
    embeddings rank, they never decide ("brouillon" ranks draft third). near: where call 1 places the value (a
    column "t_x.c_y" or a table "t_x"), searched first; codes are never compared (a digit or under 3 letters: "L",
    "MUG-RED"): they have no meaning to translate and sit close to everything.
    The values are the profile's (a column with few of them: colours, statuses); each is embedded once per process."""
    encoder()
    keys = [(f"{tid}.{cid}", str(v)) for tid, tname, cid, cname in text_columns(schema)
            for v in listed(db, tname, cname) if not code(str(v))]
    if new := sorted({v for _, v in keys} - set(_VECTORS)):
        _VECTORS.update(zip(new, _ENCODER.encode(new, normalize_embeddings=True)))
    _POOL = {k: _VECTORS[k[1]] for k in keys}
    queries = list(dict.fromkeys([text] + re.findall(r"[^\W\d_]{3,}", text)))
    qv = _ENCODER.encode(queries, normalize_embeddings=True)
    pool = {k: v for k, v in _POOL.items() if near and (k[0] == near or k[0].split(".")[0] == near)} or _POOL
    scored = []
    for (col, value), vec in pool.items():
        sims = qv @ vec
        best = int(sims.argmax())
        scored.append((float(sims[best]), col, value, queries[best]))
    scored.sort(reverse=True)
    if not scored or scored[0][0] < SEMANTIC_MIN:
        return []
    return [{"column": col, "match": "meaning", "stored": [value], "searched": q, "similarity": round(sim, 2)}
            for sim, col, value, q in scored[:SEMANTIC_TOP]]


def code(value: str) -> bool:
    """A code, not a word: a digit, under 3 letters ("L"), or capitals joined by signs ("MUG-RED", "TEE_BLK")."""
    return bool(re.search(r"\d", value)) or len(re.sub(r"[\W\d_]", "", value)) < 3 or \
        bool(re.fullmatch(r"[A-Z]+(?:[-_/.][A-Z]+)+", value))


def option(f: dict) -> str:
    """A candidate's key in call 2: its column, or column=value for a meaning match (several values of one column)."""
    return f"{f['column']}={f['stored'][0]}" if f["match"] == "meaning" else f["column"]


def within(db, schema: list[dict], text: str) -> list[dict]:
    """Stored values written whole inside `text` ("Stationery products" → Stationery), every table; the longest
    stored value wins ("Casa Living product": Casa Living, not the brand Casa).
    ponytail: one stored value per phrase; two ("the red Orbit tees") need a split, add when a case needs it."""
    hits = []
    for tid, tname, cid, cname in text_columns(schema):
        for v in listed(db, tname, cname):
            v = str(v)
            if len(re.sub(r"[\W\d_]", "", v)) >= 3 and re.search(rf"\b{re.escape(v.casefold())}\b", text.casefold()):
                hits.append((len(v), f"{tid}.{cid}", v))
    best = max((n for n, _, _ in hits), default=0)
    out = {}
    for n, col, v in hits:
        if n == best:
            out.setdefault(col, []).append(v)
    return [{"column": c, "match": "within", "stored": sorted(vs)} for c, vs in out.items()]


def state(prompt, requests, schema, ps) -> dict:
    """`stored_in`: SQLite's evidence for a phrase the linker matched ("PaperHub" → suppliers.name, products.brand)."""
    names = {f"{t['definition']['table_id']}.{c['column_id']}": f"{t['definition']['sql_name']}.{c['sql_name']}"
             for t in schema for c in t["definition"]["columns"]}

    def evidence(p):
        return [f"{names[f['column']]} = {', '.join(f['stored'][:3])} ({f['match']})" for f in p.get("linked", [])]
    return {"prompt": prompt,
            "requests": {r["id"]: ({"text": r["text"], "statement": r["statement"]} if r.get("statement") else r["text"])
                         for r in requests},                  # step 2's statement: an insert writes its values
            "tables": {t["definition"]["sql_name"]: [c["sql_name"] for c in t["definition"]["columns"]] for t in schema},
            "phrases": {p["id"]: {"text": p["text"], "request": p["request"],
                                  **({"stored_in": evidence(p)} if evidence(p) else {})} for p in ps}}


def named_options(schema) -> dict:
    opts = {}
    for t in schema:
        d = t["definition"]
        for c in d["columns"]:
            opts[f"{d['table_id']}.{c['column_id']}"] = f"The {c['sql_name']} column of {d['sql_name']}."
        opts[d["table_id"]] = f"A {d['sql_name']} record, its column not said."
    return opts | {"not_said": "Not said: nothing ties the value to a column or a kind of record."}


def questions_1(ps, schema) -> dict:
    q, named = {}, named_options(schema)
    for p in ps:
        said = f"phrase {p['id']} ('{p['text']}', in request {p['request']})"
        q[f"{p['id']}_role"] = choice(CONTEXT, f"What is {said}?", ROLES, ROLE_RULES)
        q[f"{p['id']}_named"] = choice(CONTEXT, f"Where does the prompt say {said} is stored?", named, NAMED_RULES)
        q[f"{p['id']}_written"] = yes_no(CONTEXT, f"Does a word of the prompt name the column of {said}?",
                                         WRITTEN_RULES)
        q[f"{p['id']}_own"] = yes_no(CONTEXT, f"Is {said} the name of one record of the kind the prompt says, rather "
                                              f"than a value describing other records?", OWN_RULES)
    return q


def same_key(schema, probs: dict) -> str | None:
    """The key column Jev's split answer names: a foreign key and the key it references hold the same value
    ("variant 101": sales.variant_id 0.55, variants.variant_id 0.35), so their answers add up; their best one when
    the sum is sure."""
    if not schema:
        return None
    of = {}
    for t in schema:
        d = t["definition"]
        for c in d.get("primary_key", []):
            of[f"{d['table_id']}.{c}"] = f"{d['table_id']}.{c}"
        for fk in d.get("foreign_keys", []):
            for c, rc in zip(fk["columns"], fk["references"]["columns"]):
                of[f"{d['table_id']}.{c}"] = f"{fk['references']['table']}.{rc}"
    sums = {}
    for k, p in probs.items():
        if k in of:
            sums[of[k]] = sums.get(of[k], 0) + p
    best = max(sums, key=sums.get, default=None)
    if best is None or sums[best] < SURE:
        return None
    return max((k for k in probs if of.get(k) == best), key=probs.get)


def build(ps: list[dict], a1: dict, candidates: dict, a2: dict, schema=None) -> dict:
    """ps: phrases; a1: call 1; candidates: {phrase id: search result}; a2: call 2."""
    out, warnings = [], []
    for p in ps:
        v, key = dict(p), p.get("from_phrase", p["id"])      # a leftover takes its phrase's role, and is searched
        role, rp = a1.get(f"{key}_role", (None, {}))
        v["role"] = role
        if rp.get(role, 0) < SURE:
            v["flagged"] = True
            warnings.append({"value": p["id"], "problem": "role uncertain", "answer": role,
                             "probability": round(rp.get(role, 0), 2)} | question(a1, f"{key}_role"))
        if role == "lookup":
            named, np_ = a1.get(f"{p['id']}_named", ("not_said", {}))
            if "from_phrase" not in p and given(a1, p["id"]):   # the user's column: not searched
                v |= {"column": named, "match": "given"}
                if np_.get(named, 0) < SURE:
                    v["flagged"] = True
                    warnings.append({"value": p["id"], "problem": "column uncertain", "answer": named,
                                     "probability": round(np_.get(named, 0), 2)} | question(a1, f"{p['id']}_named"))
            else:
                found = candidates.get(p["id"], [])
                if not found and (k := same_key(schema, np_)):   # an id: never found as a text, its key said
                    v |= {"column": k, "match": "given"}
                elif not found:
                    v |= {"column": None, "match": "none", "stored": []}
                    warnings.append({"value": p["id"], "problem": "not found in the store", "text": p["text"]})
                else:
                    pick = found[0]
                    if asked(found):
                        c, cp = a2.get(f"{p['id']}_pick", (None, {}))
                        pick = next((f for f in found if option(f) == c), None)
                        if cp.get(c, 0) < SURE:
                            v["flagged"] = True
                            warnings.append({"value": p["id"], "problem": "column choice uncertain", "answer": c,
                                             "probability": round(cp.get(c, 0), 2)}
                                            | question(a2, f"{p['id']}_pick", PICK_LABELS))
                    if pick and pick.get("widened_from"):
                        v["flagged"] = True
                        warnings.append({"value": p["id"], "problem": f"not found in {pick['widened_from']}; found in "
                                                                      f"{pick['column']}: confirm"})
                    if pick:
                        v |= {k: x for k, x in pick.items() if k not in ("widened_from", "similarity")}
                    else:                                     # refused; "text": kept as written, never dropped
                        v |= {"column": None, "match": "text" if c == "text" else "none", "stored": []}
                    if pick and pick["match"] not in ("exact", "within"):   # within: the stored value as written
                        warnings.append({"value": p["id"], "problem": f"{pick['match']} match: did you mean "
                                                                      f"{', '.join(pick['stored'][:3])}?"})
        guess = "from_phrase" in p or (p.get("linked") and all(f["match"] in ("meaning", "contains") for f in p["linked"]))
        refused = v.get("match") == "none" or ("from_phrase" in p and v.get("match") == "text")   # a leftover's
        if guess and refused:                                # words are its phrase's: kept as written = refused
            # the code's guess (a leftover, a meaning or partial match of the linker) that Jev refused: dropped, it
            # would become a condition on a value nobody wrote
            warnings[:] = [w for w in warnings if w.get("value") != p["id"]]
            continue
        out.append(v)
    return {"values": out, "warnings": warnings}


PICK_LABELS = {"none": "none of these", "text": "the words as written"}   # call 2's options, for the user


def keepable(found: list[dict]) -> bool:
    """"Keep the words as written" is offered when the user's words appear inside stored text (a partial match:
    "wire" to search) or match nothing exactly; never for a meaning-only match, where it would invent a text search
    from an ordinary word ("cables")."""
    return not all(f["match"] == "meaning" for f in found)


def asked(found: list[dict]) -> bool:
    """Call 2 is asked when several columns match, or for a meaning or partial match: embeddings rank and a part of a
    stored value may be a coincidence ("wire" in Wireless Mouse), Jev confirms."""
    return len(found) > 1 or any(f["match"] in ("meaning", "contains") for f in found)


def given(a1: dict, pid: str) -> bool:
    """The user wrote the column: Jev chose a column, and says a word of the prompt names it (a column guessed
    from the value is searched instead: "the Orbit Hoodie" is not name = 'Orbit Hoodie' until the store says so)."""
    return "." in a1.get(f"{pid}_named", ("not_said",))[0] and a1.get(f"{pid}_written", 0.0) >= 0.5


def scope(a1: dict, pid: str) -> str | None:
    """The table a search is limited to: the value is a record's own name ("the supplier Kora", "the Kora supplier")
    and Jev is sure of that record's table (the record, or one of its columns: answers split between the two still
    add up). A value describing other records ("the Kora products": products linked to Kora) is searched everywhere."""
    named, probs = a1.get(f"{pid}_named", ("not_said", {}))
    if named == "not_said" or a1.get(f"{pid}_own", 0.0) < 0.5:
        return None
    table = named.split(".")[0]
    return table if sum(p for k, p in probs.items() if k.split(".")[0] == table) >= SCOPE_MIN else None


def leftover(p: dict, part: str) -> dict | None:
    """The phrase's words around the part that matched ("PaperHub stationery" − "PaperHub" → "stationery")."""
    i = p["text"].casefold().find(part.casefold())
    pieces = [(0, i), (i + len(part), len(p["text"]))] if i >= 0 else []
    best = max(pieces, key=lambda ab: len(p["text"][ab[0]:ab[1]].strip(" ,.;:'")), default=None)
    if best is None:
        return None
    raw = p["text"][best[0]:best[1]]
    text = raw.strip(" ,.;:'")
    if len(re.sub(r"[\W\d_]", "", text)) < 3:
        return None
    start = p["start"] + best[0] + raw.index(text)
    return {"id": f"{p['id']}b", "request": p["request"], "text": text, "start": start, "end": start + len(text),
            "from_phrase": p["id"]}


def search_phrase(db, schema, p: dict, table: str | None) -> list[dict]:
    """The whole phrase; else its parts, longest first, exact or misspelled ("Casa Living products" → "Casa Living");
    else a stored value written inside it, over every table only (in a named table it would find the brand Casa in
    "Casa Living product"); else a part inside a stored value."""
    whole, parts = p["text"], p.get("parts", [])
    if found := search(db, schema, whole, table):
        return [f | {"searched": whole} for f in found]
    for text in parts:
        if found := search(db, schema, text, table, ("exact", "typo")):
            return [f | {"searched": text} for f in found]
    if table is None and (found := within(db, schema, whole)):  # "Atlas Textile item": Atlas Textile, before 'Atlas'
        return [f | {"searched": f["stored"][0]} for f in found]
    for text in parts:
        if found := search(db, schema, text, table, ("contains",)):
            return [f | {"searched": text} for f in found]
    return []


def sources_of(db, schema, prompt, requests, model, mode: str) -> list[dict]:
    """The phrases call 1 classifies. mode: "gliner" (GLiNER2 proposes, SQLite searches after), "linker" (the request's
    words matched against the store's values, linker.py: phrases come with their candidates), "both" (the linker's
    phrases, plus GLiNER2's that overlap none of them: new values and values the store lacks, "Zephyr")."""
    if mode == "gliner":
        return phrases(prompt, requests, model)
    from .linker import link
    lk = link(db, schema, prompt, requests)
    if mode == "linker":
        return lk
    gl = [g for g in phrases(prompt, requests, model)
          if all(g["end"] <= x["start"] or g["start"] >= x["end"] for x in lk)]
    return sorted(lk + gl, key=lambda p: p["start"])


async def run(jev, db, prompt: str, requests: list[dict], schema: list[dict], model, mode: str = "both") -> dict:
    """mode "both" since 2026-10-12 (v2/bench/value_linking, 233 values: both 185 right / 0 missed, recall of stored
    filters 90%; GLiNER2 alone 167 / 30 missed, 74%; the linker alone 155 / 34 missed: no absent or new values)."""
    ps = sources_of(db, schema, prompt, requests, model, mode)
    a1, a2, candidates = {}, {}, {}
    try:
        a1 = await ask(jev, state(prompt, requests, schema, ps), questions_1(ps, schema))
        for p in list(ps):
            if a1.get(f"{p['id']}_role", (None,))[0] != "lookup" or given(a1, p["id"]):
                continue
            if "linked" in p:                                 # the linker's candidates: SQLite already matched them
                table = scope(a1, p["id"])
                candidates[p["id"]] = [f for f in p["linked"] if f["column"].split(".")[0] == table] or p["linked"]
                continue
            table = scope(a1, p["id"])
            found = search_phrase(db, schema, p, table)
            if not found and table:                           # nothing in the named table: every table, flagged
                found = [f | {"widened_from": table} for f in search_phrase(db, schema, p, None)]
            if not found:                                     # no spelling matches: the meaning, Jev confirms
                named = a1.get(f"{p['id']}_named", ("not_said",))[0]
                found = meaning(db, schema, p["text"], None if named == "not_said" else named)
            candidates[p["id"]] = found
            # only a part matched: the words around it may be a second value, exact or misspelled only
            if found and found[0]["searched"] != p["text"] and (rest := leftover(p, found[0]["searched"])):
                more = [f | {"searched": rest["text"]} for f in search(db, schema, rest["text"], None, ("exact", "typo"))]
                if more := more or meaning(db, schema, rest["text"]):   # "variantes rouges de taille": rouges → red
                    ps.insert(ps.index(p) + 1, rest)
                    candidates[rest["id"]] = more
        q2 = {f"{pid}_pick": choice(CONTEXT, f"In phrase {pid} ('{next(p['text'] for p in ps if p['id'] == pid)}'), "
                                             f"which stored values does '{found[0]['searched']}' mean?",
                                    {option(f): f"{f['column'].split('.')[1][2:]} of {f['column'].split('.')[0][2:]}: "
                                                f"{', '.join(f['stored'][:5])} ({f['match']})" for f in found}
                                    | {"none": "None of these: not a value of these columns."}
                                    | ({"text": "Keep the words as written: a text to search inside a column, or a "
                                                "value the store does not hold."} if keepable(found) else {}))
              for pid, found in candidates.items() if asked(found)}
        a2 = await ask(jev, state(prompt, requests, schema, ps), q2)
    except Exception as e:                                    # API error: nothing is guessed
        a1 = a1 or {"error": repr(e)}
    section = build(ps, a1, candidates, a2, schema)
    section["jev"] = {"call_1": a1, "candidates": candidates, "call_2": a2}
    return section
