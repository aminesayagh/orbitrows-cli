"""Operations: one change request → the list of operations it contains, each an independent record (role, function,
left, right, text). Decided 2026-10-14 with the user: rules first, the spaCy parser as evidence, no Jev; what the rules
cannot settle becomes an ambiguity record, never a guess. The links (which result feeds which) stay "unresolved" unless
the operation's own words name the operand: a second pass builds the formula.

    steps: 1 parse (spaCy) · 2 number occurrences · 3–4 operation phrases and their operands (lexicon) · 5 condition
    roles · 6 normalisation · 7 records (Pydantic) · 8 checks (coverage, provenance)

Input: the request text, its language and step 0's annotations (`upstream`: the changed column, the words naming
columns, the condition phrases with their SQL). Output: {"records": [...], "ambiguities": [...], "provenance": {...}}.
"""
import functools
import re

from pydantic import BaseModel
from text_to_num import text2num

ROLES = ("where", "where_not", "branch_condition", "unresolved_condition", "branch", "calculation", "bound", "rounding",
         "guard", "assignment")
WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")
PERCENT = r"\s*(?:%|per\s?cent\b|percent\b|pour\s?cent\b|pourcent\b)"
FACTOR_VERBS = {r"halv(?:e|es|ed|ing)|in\s+half|by\s+half": 0.5, r"doubl(?:e|es|ed|ing)": 2, r"tripl(?:e|es|ed|ing)": 3,
                r"quadrupl(?:e|es|ed|ing)": 4}                  # the verb is the operation: "halve the cost"
FACTORS = {"twice": 2, "half": 0.5, "a third": 0.333333, "one third": 0.333333,
           "a quarter": 0.25, "one quarter": 0.25, "deux fois": 2, "le double": 2, "la moitié": 0.5, "moitié": 0.5,
           "un tiers": 0.333333, "un quart": 0.25}
UP = r"rais\w*|extend\w*|bump\w*|hik\w*|top\w*\s+up|lift\w*|grow\w*|gain\w*|increment\w*|push\w*\s+up|higher|bigger|increas\w*|add\w*|plus|up|more|mark\w*\s+up|boost\w*|augment\w*|hausse\w*|ajout\w*|major\w*|monte\w*|gets?|receiv\w*|reçoi\w*|reçu\w*"
DOWN = r"lower\w*|deduct\w*|trim\w*|shav\w*|knock\w*\s+off|slash\w*|bring\w*\s+down|mark\w*\s+down|shrink\w*|cheaper|smaller|fewer|tak\w*|reduc\w*|cut\w*|decreas\w*|off|discount\w*|minus|less|remov\w*|subtract\w*|tak\w*\s+away|drop\w*|baiss\w*|rédui\w*|diminu\w*|remise|réduction|moins|enlev\w*|retir\w*|soustr\w*"
SET = (r"\b(?:set|change|put|make|fix|reset|mettre|mets|mettez|fixe\w*|passe\w*|règle\w*)\b[^,;.]*?\b(?:to|at|à|a)\s*$|"
       r"\breplac\w*\b[^,;.]*?\bwith\s*$|\bmake\s+(?:the\s+|its\s+|their\s+)?\w+(?:\s+\w+)?\s*$")
GUARD_MAX = (r"\b(?:do\s+not|don't|never)\s+let\s+(?:it|them)\s+(?:exceed\w*|(?:go|rise|climb)\s+(?:above|over))\b|"
             r"\bnever\s+(?:go\s+|be\s+|rise\s+)?(?:above|over|exceed\w*|more\s+than|higher\s+than)\b|\bat\s+most\b|"
             r"\bno\s+more\s+than\b|\bno\s+higher\s+than\b|\bnot\s+(?:go\s+)?(?:above|over|exceed\w*)\b|\blimit\w*\s+(?:it\s+|them\s+)?(?:to|at)\b|\bcapped\s+at\b|\bcap\s+(?:it|them)?\s*at\b|\bmaximum\s+(?:of\s+)?|"
             r"\bjamais\s+(?:au-dessus\s+de|plus\s+de)|\bsans\s+dépasser\b|\bau\s+plus\b|\bau\s+maximum\b|\bplafond\w*\s+(?:à|de)\b")
GUARD_MIN = (r"\b(?:do\s+not|don't|never)\s+let\s+(?:it|them)\s+(?:drop|fall|go|sink)\s+(?:below|under)\b|"
             r"\bnever\s+(?:go\s+|be\s+|fall\s+|drop\s+)?(?:below|under|less\s+than|lower\s+than)\b|\bat\s+least\b|"
             r"\bno\s+less\s+than\b|\bno\s+lower\s+than\b|\bnot\s+(?:go\s+|fall\s+|drop\s+)?(?:below|under)\b|\bminimum\s+(?:of\s+)?|\bfloor\s+(?:of|at)\b|"
             r"\bjamais\s+(?:en\s+dessous\s+de|sous|moins\s+de)|\bau\s+moins\b|\bau\s+minimum\b|\bpas\s+moins\s+de\b")
GUARD_OR = r"\b(?:or|ou)\s+(?:(?:go|be)\s+)?(above|over|below|under|au-dessus\s+de|en\s+dessous\s+de|sous)\b"
KEEP_OLD = r"\bkeep\s+the\s+(?:old|current|previous)\s+\w+\s+if\s+the\s+new\s+\w+\s+(?:is|are)\s+(lower|higher)|\bgarde\w*\s+l'ancien\s+\w+\s+si\s+le\s+nouveau\s+est\s+plus\s+(bas|haut)"
KEEP = r"\b(?:leave|leaving|keep|keeping|stay\w*|remain\w*|untouched|unchanged|as\s+(?:they\s+are|is)|don't\s+(?:change|touch)|do\s+not\s+(?:change|touch)|laiss\w*|gard\w*|rest\w*|inchangé\w*|ne\s+(?:change|touche)\w*\s+pas)\b"
CUE = r"\b(?:but|except|excluding|apart\s+from|other\s+than|unless|only|otherwise|whereas|while|which|mais|sauf|excepté|hormis|à\s+part|sinon|seulement|uniquement|qui|tandis)\b"
UNLESS = r"\b(?:unless|à\s+moins\s+que|sauf\s+si)\b"
OTHERWISE = r"\b(?:otherwise|else|sinon|autrement)\b"
# contrast evidence: the request has more than one formula (English, 2026-10-14)
CONTRAST = (r"\b(?:but|except|unless|otherwise|instead|only|whereas|while|where|for\s+all\s+others|the\s+others|"
            r"the\s+rest|everything\s+else|sinon|sauf|mais)\b")
ALTERNATIVE = r"\b(?:otherwise|else|instead|for\s+all\s+others|the\s+others|the\s+rest|everything\s+else|sinon)\b"
LEAD = r"\b(?:for|except|unless|but|sauf|pour|mais)\b"   # a cue leading a condition ("For Casablanca, …")


class Record(BaseModel):
    role: str
    function: str | None
    left: dict | None
    right: dict | None
    text: str
    start: int
    end: int
    rule_id: str
    occurrences: list[str] = []
    status: str = "accepted"


@functools.cache
def nlp(lang: str):
    import spacy
    return spacy.load({"en": "en_core_web_sm", "fr": "fr_core_news_sm"}[lang])


# --- step 2: number occurrences ----------------------------------------------------------------------------------

def occurrences(text: str, lang: str, skip: list[tuple[int, int]]) -> list[dict]:
    """Every number written in the text (digits, number words, factor words), with offsets; numbers inside a condition
    phrase are the condition's (skip)."""
    out = []
    for m in re.finditer(rf"(?<![\w.])(\d+(?:[.,]\d+)?)(?!\w)({PERCENT})?", text, re.I):
        out.append({"text": m.group(0).strip(), "start": m.start(), "end": m.start() + len(m.group(0).rstrip()),
                    "value": float(m.group(1).replace(",", ".")), "percent": bool(m.group(2)), "kind": "digits"})
    for pat, v in FACTOR_VERBS.items():
        for m in re.finditer(rf"\b(?:{pat})\b", text, re.I):
            out.append({"text": m.group(0), "start": m.start(), "end": m.end(), "value": v, "percent": False,
                        "kind": "factor_verb"})
    for phrase, v in sorted(FACTORS.items(), key=lambda kv: -len(kv[0])):
        for m in re.finditer(rf"\b{re.escape(phrase)}\b", text, re.I):
            if not any(o["start"] <= m.start() < o["end"] for o in out):
                out.append({"text": m.group(0), "start": m.start(), "end": m.end(), "value": v, "percent": False,
                            "kind": "factor"})
    for m in WORD.finditer(text):
        w = m.group(0).casefold()
        if w in ("a", "an", "one", "un", "une", "i") or any(o["start"] <= m.start() < o["end"] for o in out):
            continue
        try:
            v = float(text2num(w, lang))
        except ValueError:
            continue
        pct = re.match(PERCENT, text[m.end():], re.I)
        before = text[:m.start()].split()[-1:] or [""]
        if pct or before[0].casefold() in ("by", "to", "of", "de", "à", "par", "at", "than", "plus", "minus", "times", "fois"):
            end = m.end() + (len(pct.group(0)) if pct else 0)
            out.append({"text": text[m.start():end], "start": m.start(), "end": end, "value": v, "percent": bool(pct),
                        "kind": "words"})
    out = [o for o in out if not any(s <= o["start"] < e for s, e in skip)]
    out.sort(key=lambda o: o["start"])
    for i, o in enumerate(out, 1):
        o["occ"] = f"n{i}"
        o["value"] = int(o["value"]) if float(o["value"]).is_integer() else o["value"]
    return out


# --- helpers ------------------------------------------------------------------------------------------------------

def segments(text: str) -> list[tuple[int, int]]:
    """Clauses: split at , ; : . (a clause starting with which / qui is joined to the one before, for the roles)."""
    out, s = [], 0
    for m in re.finditer(r"[,;:.](?:\s|$)", text):
        if m.start() > s:
            out.append((s, m.start()))
        s = m.end()
    if s < len(text):
        out.append((s, len(text)))
    return out


def segment_of(segs, pos):
    return next(((s, e) for s, e in segs if s <= pos < e), (0, len(""))) if segs else (0, 0)


def column_mentions(text: str, columns: list[dict]) -> list[dict]:
    out = []
    for c in columns:
        for m in re.finditer(rf"\b{re.escape(c['text'])}\b", text, re.I):
            out.append({"start": m.start(), "end": m.end(), "column": c["column"], "text": m.group(0)})
    out.sort(key=lambda x: (x["start"], -x["end"]))
    return [x for i, x in enumerate(out) if not any(y["start"] <= x["start"] and x["end"] <= y["end"] and y is not x
                                                    and (y["end"] - y["start"]) > (x["end"] - x["start"]) for y in out)]


def nearest(pattern: str, text: str, lo: int, hi: int, pos: int):
    """The match of pattern in text[lo:hi] closest to pos (its distance, the match)."""
    best = None
    for m in re.finditer(pattern, text[lo:hi], re.I):
        a, b = lo + m.start(), lo + m.end()
        d = pos - b if b <= pos else a - pos
        if best is None or d < best[0]:
            best = (d, a, b)
    return best


def col_after(cols, text, pos, hi):
    """The column named right after pos ("Round **the price**", "off **the prices**", "to **weight**")."""
    m = re.match(r"\s*(?:the\s+|their\s+|its\s+|all\s+|each\s+|every\s+)?", text[pos:hi], re.I)
    return next((c for c in cols if c["start"] == pos + m.end()), None)


def col_in(cols, a, b):
    """A column word inside text[a:b] (the last one: the closest to the number)."""
    inside = [c for c in cols if a <= c["start"] and c["end"] <= b]
    return inside[-1] if inside else None


# --- steps 3–6: operations ----------------------------------------------------------------------------------------

def extract(text: str, upstream: dict, lang: str = "en") -> dict:
    preds = []
    for p in upstream.get("predicates", []):
        i = text.find(p["text"])
        preds.append(p | {"start": i, "end": i + len(p["text"])})
    doc = nlp(lang)(text)                                        # step 1: the parse (evidence for the assignment)
    occs = occurrences(text, lang, [(p["start"], p["end"]) for p in preds if p["start"] >= 0])
    cols = column_mentions(text, upstream.get("columns", []))
    cols = [c for c in cols if not any(p["start"] <= c["start"] < p["end"] for p in preds)]
    segs = segments(text)
    records, ambiguities = [], []

    def add(role, function, left, right, a, b, rule, occ=()):
        records.append(Record(role=role, function=function, left=left, right=right, text=text[a:b].strip(" ,"),
                              start=a, end=b, rule_id=rule, occurrences=list(occ)))

    # guards first: their zones decide which calculations only compute a limit (bounds)
    guards = []
    for kind, pat in (("clamp_max", GUARD_MAX), ("clamp_min", GUARD_MIN)):
        for m in re.finditer(pat, text, re.I):
            guards.append([kind, m.start(), m.end(), f"guard.{'never_above' if kind == 'clamp_max' else 'never_below'}"])
    for m in re.finditer(GUARD_OR, text, re.I):
        if any(g[2] <= m.start() and m.start() - g[2] < 60 for g in guards):
            word = m.group(1).casefold()
            kind = "clamp_max" if word in ("above", "over") or word.startswith("au-dessus") else "clamp_min"
            guards.append([kind, m.start() + m.group(0).casefold().find(word), m.end(), "guard.or_" + kind[6:]])
    for m in re.finditer(KEEP_OLD, text, re.I):
        target = next((c for c in cols if c["column"] == upstream["target"]), None)
        kind = "clamp_min" if m.group(1) or m.group(2) in ("lower", "bas") else "clamp_max"
        add("guard", "clamp_min" if (m.group(1) or m.group(2)) in ("lower", "bas") else "clamp_max",
            {"unresolved": True}, {"column": upstream["target"]} if target else {"unresolved": True},
            m.start(), m.end(), "guard.keep_old")
    guards.sort(key=lambda g: g[1])
    zones = []
    for i, (kind, a, b, rule) in enumerate(guards):
        seg = segment_of(segs, a)
        end = min([g[1] for g in guards[i + 1:]] + [seg[1]])
        zones.append((kind, a, b, end, rule))

    def zone_of(pos):
        return next((z for z in zones if z[2] <= pos < z[3]), None)

    used_dir = {}                                               # occurrence → its direction (for "apply the 15%")
    for o in occs:
        seg = segment_of(segs, o["start"])
        pre, post = text[seg[0]:o["start"]], text[o["end"]:seg[1]]
        z = zone_of(o["start"])
        role = "bound" if z else "calculation"
        # rounding
        if re.search(r"\bnearest\s*(?:multiple\s+of\s*)?$|\bplus\s+proche\b", pre, re.I) or \
                (re.search(r"\barrondi\w*\b", pre, re.I) and re.match(r"\s*(?:le\s+plus\s+proche)", post, re.I)):
            r0 = nearest(r"\bround\w*|\barrondi\w*", text, seg[0], o["start"], o["start"])
            c = r0 and col_after(cols, text, r0[2], o["start"])
            add("rounding", "round_to", {"column": c["column"]} if c else {"unresolved": True},
                {"value": o["value"], "source": o["text"]}, r0[1] if r0 else o["start"], o["end"], "round.nearest", [o["occ"]])
            continue
        if re.match(r"\s*(?:decimal|décimale|dp\b)", post, re.I):
            r0 = nearest(r"\bround\w*|\barrondi\w*", text, seg[0], o["start"], o["start"])
            m = re.match(r"\s*(?:decimal|décimale)s?", post, re.I)
            c = r0 and col_after(cols, text, r0[2], o["start"])
            add("rounding", "round", {"column": c["column"]} if c else {"unresolved": True},
                {"value": o["value"], "source": o["text"]}, r0[1] if r0 else o["start"], o["end"] + (m.end() if m else 0),
                "round.decimals", [o["occ"]])
            continue
        # a guard's literal limit: "never below 20"
        if z and not text[z[2]:o["start"]].strip(" ,") and o["kind"] not in ("factor", "factor_verb") and not o["percent"]:
            z_rec = z
            continue_guard = {"value": o["value"], "source": o["text"]}
            add("guard", z_rec[0], {"unresolved": True}, continue_guard, z_rec[1], o["end"], z_rec[4] + ".literal",
                [o["occ"]])
            zones.remove(z_rec)
            zones.append((z_rec[0], z_rec[1], z_rec[2], z_rec[2], "done"))   # its record is written
            continue
        # factors: twice their cost, half of it, by a third
        if o["kind"] == "factor_verb":
            c = col_after(cols, text, o["end"], seg[1])
            a, b = o["start"], (c["end"] if c else o["end"])
            if not c:                                           # the column before: "the quantity in half", "is doubled"
                m = re.search(r"(\S+)\s+(?:(?:is|are|gets?|has\s+been|have\s+been|be)\s+)?$", pre, re.I)
                c = m and next((x for x in cols if x["end"] == seg[0] + m.end(1)), None)
                a = c["start"] if c else a
            add(role, "*", {"column": c["column"]} if c else {"unresolved": True}, {"value": o["value"], "source": o["text"]},
                a, b + (len(m.group(0)) if not c and (m := re.match(r"\s+(?:it|them)\b", text[b:seg[1]], re.I)) else 0),
                "factor.verb", [o["occ"]])
            continue
        if o["kind"] == "factor":
            after = col_in(cols, o["end"], o["end"] + 16)
            d = nearest(rf"\b(?:{UP})\b", text, seg[0], o["start"], o["start"])
            dn = nearest(rf"\b(?:{DOWN})\b", text, seg[0], o["start"], o["start"])
            by = re.search(r"\b(?:by|de|d')\s*$", pre, re.I)
            if by and (d or dn) and o["value"] < 1:
                up = d and (not dn or d[0] <= dn[0])
                v = round(1 + o["value"], 6) if up else round(1 - o["value"], 6)
                c = col_in(cols, (d if up else dn)[1], o["start"])
                add(role, "*", {"column": c["column"]} if c else {"unresolved": True}, {"value": v, "source": o["text"]},
                    (d if up else dn)[1], o["end"], "factor.by_fraction", [o["occ"]])
                continue
            add(role, "*", {"column": after["column"]} if after else {"unresolved": True},
                {"value": o["value"], "source": o["text"]}, o["start"], after["end"] if after else o["end"] +
                (len(re.match(r"\s*(?:of\s+it|of\s+them|de\s+\w+)?", post, re.I).group(0))),
                "factor.word", [o["occ"]])
            continue
        # percent
        if o["percent"]:
            p = o["value"] / 100
            m = re.match(r"\s*(?:of|du|de\s+la|des|de)\s+(?:the\s+|their\s+|its\s+|le\s+|la\s+|leur\s+)?", post, re.I)
            c_of = m and next((c for c in cols if c["start"] == o["end"] + m.end()), None)
            if c_of:                                            # "10% of the cost": a part of that column
                add(role, "*", {"column": c_of["column"]}, {"value": round(p, 6), "source": o["text"]},
                    o["start"], c_of["end"], "percent.of_column", [o["occ"]])
                verb = nearest(rf"\b(?:{UP}|{DOWN})\b", text, seg[0], o["start"], o["start"])
                if verb and not z:
                    c = col_in(cols, verb[1], o["start"])
                    m2 = re.match(r"\s*(?:to|into|on)\b", text[c_of["end"]:seg[1]], re.I)
                    c = c or (m2 and col_after(cols, text, c_of["end"] + m2.end(), seg[1]))
                    up = re.fullmatch(rf"(?:{UP})", text[verb[1]:verb[2]], re.I) is not None
                    add(role, "+" if up else "-", {"column": c["column"]} if c else {"unresolved": True},
                        {"unresolved": True}, verb[1], c["end"] if c else verb[2], "percent.of_column.apply")
                continue
            cp = re.search(r"(\b\w+)\s+(plus|\+|minus|moins)\s*$", pre, re.I)
            c_plus = cp and next((c for c in cols if c["end"] == seg[0] + cp.end(1)), None)
            if c_plus:                                          # "the cost plus 10%"
                v = 1 + p if cp.group(2).casefold() in ("plus", "+") else 1 - p
                add(role, "*", {"column": c_plus["column"]}, {"value": round(v, 6), "source": o["text"]},
                    c_plus["start"], o["end"], "percent.column_plus", [o["occ"]])
                continue
            m = re.match(r"\s*(above|over|more\s+than|below|under|less\s+than|au-dessus\s+d[eu]|en\s+dessous\s+d[eu])\s+"
                         r"(?:the\s+|their\s+|its\s+|le\s+|la\s+)?", post, re.I)
            c_rel = m and next((c for c in cols if c["start"] == o["end"] + m.end()), None)
            if c_rel:                                           # "30% above cost"
                up = m.group(1).casefold().startswith(("above", "over", "more", "au-dessus"))
                add(role, "*", {"column": c_rel["column"]}, {"value": round(1 + p if up else 1 - p, 6), "source": o["text"]},
                    o["start"], c_rel["end"], "percent.relative_column", [o["occ"]])
                continue
        cp = re.search(r"(\b\w+)\s+(plus|\+|minus|-)\s*$", pre, re.I)
        c_plus = cp and next((c for c in cols if c["end"] == seg[0] + cp.end(1)), None)
        if c_plus and not o["percent"]:
            add(role, "+" if cp.group(2).casefold() in ("plus", "+") else "-", {"column": c_plus["column"]},
                {"value": o["value"], "source": o["text"]}, c_plus["start"], o["end"], "amount.column_plus", [o["occ"]])
            continue
        # direction (percent or amount): the closest up / down word in the clause
        # before the number: any up / down word of the clause; after it: only one attached to it ("15% off")
        d_up = nearest(rf"\b(?:{UP})\b", text, seg[0], o["start"], o["start"])
        d_dn = nearest(rf"\b(?:{DOWN})\b", text, seg[0], o["start"], o["start"])
        after = re.match(r"\s*(?:%s)?\s*(off|discounts?|cuts?|reductions?|markdowns?|drops?|decreases?|hikes?|increases?|raises?|markups?|rises?|remise|de\s+réduction|de\s+rabais|less|moins|more|de\s+plus|up|down|higher|lower|cheaper|bigger|smaller)\b"
                         % ("MAD|dirhams?|units?|unités?|grams?|g|kg|days?|items?|pieces?"), text[o["end"]:seg[1]], re.I)
        if after:
            w = after.group(1).casefold()
            hit = (0, o["end"] + after.start(1), o["end"] + after.end(1))
            if re.fullmatch(rf"(?:{DOWN})|down|de\s+rabais|cuts?|reductions?|markdowns?|drops?|decreases?", w, re.I):
                d_dn, d_up = hit, None if not d_up or d_up[0] > 0 else d_up
            else:
                d_up, d_dn = hit, None if not d_dn or d_dn[0] > 0 else d_dn
        divide = re.search(r"\b(?:divid\w*|divis\w*)\s+(?:\w+\s+){0,4}(?:by|par)\s*$", pre, re.I)
        times = re.search(r"\b(?:multipl\w*)\s+(?:\w+\s+){0,4}(?:by|par)\s*$|\btimes\s*$|\bfois\s*$", pre, re.I)
        if not o["percent"] and (divide or times):
            w = divide or times
            add(role, "/" if divide else "*", {"column": c["column"]} if (c := col_in(cols, seg[0] + w.start(), o["start"])) else {"unresolved": True},
                {"value": o["value"], "source": o["text"]}, seg[0] + w.start(), o["end"], "amount.divide" if divide else "amount.times",
                [o["occ"]])
            continue
        if not o["percent"] and re.search(SET, pre, re.I):
            s = re.search(SET, pre, re.I)
            add(role, "set", None, {"value": o["value"], "source": o["text"]}, seg[0] + s.start(), o["end"], "amount.set",
                [o["occ"]])
            continue
        cands = [(d[0], d, True) for d in [d_up] if d] + [(d[0], d, False) for d in [d_dn] if d]
        if cands:
            _, d, up = min(cands, key=lambda x: x[0])
            rule = "dir"
        else:                                                   # no direction word: reuse one ("apply the 15%")
            same = [x for x in used_dir.values() if x["text"].replace("the ", "") == o["text"]]
            other = [x for x in used_dir.values() if x["percent"] == o["percent"]] or list(used_dir.values())
            src = (same or other)
            if not src:
                ambiguities.append({"problem": "no direction for this number", "occurrence": o["occ"], "text": o["text"],
                                    "start": o["start"], "end": o["end"], "step": "3 operation phrases"})
                continue
            d, up, rule = None, src[0]["up"], "reuse"
        used_dir[o["occ"]] = {"text": o["text"], "percent": o["percent"], "up": up}
        a = min(d[1], o["start"]) if d else o["start"]
        b = max(d[2], o["end"]) if d else o["end"]
        lo, hi = (d[1], o["start"]) if d and d[1] < o["start"] else (o["start"], o["start"])
        c = col_in(cols, lo, hi)
        if not c:                                               # "off the prices", "le prix", "to the price"
            m = re.match(r"\s*(?:(?:units?|items?|pieces?|grams?|g|kg|MAD|dirhams?|days?)\s+)?(?:more\s+)?"
                         r"(?:off|to|into|from|on|of|sur|à|au|aux|de|du|des)?\s*(?:the\s+|their\s+|its\s+|le\s+|la\s+|les\s+|l')?",
                         text[b:seg[1]], re.I)
            c = next((x for x in cols if x["start"] == b + m.end()), None)
            if c:
                b = c["end"]
        if not c and d and d[1] > o["start"] and re.fullmatch(r"higher|lower|cheaper|bigger|smaller", text[d[1]:d[2]], re.I):
            # "Make the price 10% higher": a comparative after the number, the column named before it
            c = col_in(cols, seg[0], o["start"])
        if c:
            a = min(a, c["start"])
            b = max(b, c["end"])
        m = re.match(r"\s*(?:discounts?|off|remise|de\s+réduction)\b", text[o["end"]:seg[1]], re.I)
        if m and not d:
            b = o["end"] + m.end()
        if o["percent"]:
            fn, v = "*", round(1 + o["value"] / 100 if up else 1 - o["value"] / 100, 6)
        else:
            fn, v = ("+" if up else "-"), o["value"]
        kind = "percent" if o["percent"] else "amount"
        add(role, fn, {"column": c["column"]} if c else {"unresolved": True}, {"value": v, "source": o["text"]},
            a, b, f"{kind}.{'up' if up else 'down'}" + (".reuse" if rule == "reuse" else ""), [o["occ"]])

    # "plus the quantity": a column added to a result
    for m in re.finditer(r"\b(plus|minus|add|adding|moins|ajout\w*)\s+(?:the\s+|le\s+|la\s+)?", text, re.I):
        c = next((x for x in cols if x["start"] == m.end()), None)
        if c and not any(r.start <= c["start"] < r.end for r in records):
            add("bound" if zone_of(m.start()) else "calculation",
                "-" if m.group(1).casefold() in ("minus", "moins") else "+",
                {"unresolved": True}, {"column": c["column"]}, m.start(), c["end"], "column.plus")

    # guards whose limit is computed (written as a bound record above), or a named column ("no less than cost")
    for kind, a, b, end, rule in zones:
        if rule != "done":
            c = col_after(cols, text, b, end)
            alone = c and not any(c["start"] < o["start"] < end for o in occs) and \
                not re.match(r"\s*(?:plus|minus|times|\+|-)", text[c["end"]:end], re.I)
            add("guard", kind, {"unresolved": True}, {"column": c["column"]} if alone else {"unresolved": True},
                a, c["end"] if alone else b, rule + (".column" if alone else ""))
    for m in re.finditer(r"\bround(?:s|ed|ing)?\b", text, re.I):
        if not any(r.role == "rounding" and r.start <= m.start() < r.end for r in records):
            seg = segment_of(segs, m.start())
            c = col_after(cols, text, m.end(), seg[1])
            add("rounding", "round", {"column": c["column"]} if c else {"unresolved": True}, None, m.start(),
                c["end"] if c else m.end(), "round.plain")
    for m in re.finditer(r"\b(?:set|change|make|reset)\b[^,;.]*?\b(?:to|at|equal\s+to)\s+", text, re.I):
        seg = segment_of(segs, m.start())
        c = col_after(cols, text, m.end(), seg[1])
        if c and c["column"] != upstream["target"] and not re.match(r"\s*(?:plus|minus|times|\+|-|\d)", text[c["end"]:seg[1]], re.I) \
                and not any(r.start <= c["start"] < r.end for r in records):
            add("calculation", "set", None, {"column": c["column"]}, m.start(), c["end"], "set.column")

    # step 5: the role of each condition
    actions = [r for r in records if r.role in ("calculation", "rounding")]
    first = min(actions, key=lambda r: r.start) if actions else None
    found = []
    for p in preds:
        if p["start"] < 0:
            ambiguities.append({"problem": "condition phrase not found in the text", "pred": p["id"], "step": "0 upstream"})
            continue
        s0, e = segment_of(segs, p["start"])
        cue = re.search(CUE, text[s0:p["start"]], re.I)
        lead = [m for m in re.finditer(LEAD, text[s0:p["start"]], re.I)]
        lead = lead[-1] if lead else None

        def owned(lo, hi):
            start = lo + cue.start() if cue else lo
            return [r for r in actions if lo <= r.start < hi and r.start >= start]

        own = owned(s0, e)
        nxt = next(((x, y) for x, y in segs if x >= e), None)
        if nxt and nxt[0] - e <= 2 and (re.match(r"\s*(?:which|who|that|where|then|qui)\b", text[nxt[0]:nxt[1]], re.I)
                                        or (not own and lead)):
            e = nxt[1]                                         # "For Casablanca, | set reserved to 6"
            own = owned(s0, e)
        found.append((p, s0, e, cue, lead, own))
    # parallel owners: conditions with an action of their own and no contrast word ("… for white variants; blue
    # ones get 7 off"); one introduced by "but / except" is a subset of the others, not a competitor
    parallel = [p["id"] for p, s0, e, cue, lead, own in found if own and not re.search(CONTRAST, text[s0:e], re.I)]
    for p, s0, e, cue, lead, own in found:
        clause = text[s0:e]
        outside = [r for r in actions if r not in own]
        later = first is not None and not (s0 <= first.start < e)
        # contrast in its own clause ("but for the black ones apply …"), or an alternative written elsewhere
        # ("For Casablanca, set … ; otherwise add 1")
        contrast = re.search(CONTRAST, clause, re.I) is not None or \
            re.search(ALTERNATIVE, text[:s0] + " " * (e - s0) + text[e:], re.I) is not None
        if re.search(KEEP, clause, re.I) and not own:
            role, rule = "where_not", "cond.keep"
        elif re.search(UNLESS, text[s0:p["start"]], re.I) and not own:
            role, rule = "where_not", "cond.unless"
        elif own and outside and (contrast or any(x != p["id"] for x in parallel) and p["id"] in parallel):
            role, rule = "branch_condition", "cond.contrast" if contrast else "cond.competing"
        elif own and outside and later:                         # "…, then halve it for black variants": that step
            role, rule = "unresolved_condition", "cond.later_step"   # only, or the whole request? the text doesn't say
        elif cue and re.match(r"(?:except|excluding|sauf|excepté|hormis|apart|other)", cue.group(0), re.I):
            role, rule = "unresolved_condition", "cond.except_without_action"
        else:
            role, rule = "where", "cond.in_request"
        add(role, None, {"pred": p["id"]}, None, p["start"], p["end"], rule)
        if role == "branch_condition":
            add("branch", "case", {"pred": p["id"]}, None, s0 + lead.start() if lead else p["start"], p["end"], "branch.case")

    # the assignment: the words saying which column changes
    t = next((c for c in cols if c["column"] == upstream["target"]), None)
    if t:
        verbs = [w for w in doc if w.idx < t["start"] and (w.pos_ in ("VERB", "AUX") or w.dep_ == "ROOT")]
        seg = segment_of(segs, t["start"])
        verbs = [w for w in verbs if w.idx >= seg[0]]
        a = verbs[-1].idx if verbs else seg[0]
        add("assignment", "assign", {"column": upstream["target"]}, {"unresolved": True}, a, t["end"], "target.verb")
    else:
        ambiguities.append({"problem": "the changed column is not named", "step": "7 records"})

    # step 8: coverage and provenance
    prov = {o["occ"]: [i for i, r in enumerate(records) if o["occ"] in r.occurrences] for o in occs}
    for o in occs:
        if not prov[o["occ"]] and not any(x.get("occurrence") == o["occ"] for x in ambiguities):
            ambiguities.append({"problem": "number attached to no operation", "occurrence": o["occ"], "text": o["text"],
                                "start": o["start"], "end": o["end"], "step": "3 operation phrases"})
    records.sort(key=lambda r: r.start)
    return {"records": [r.model_dump() for r in records], "ambiguities": ambiguities, "provenance": prov,
            "occurrences": occs}
