"""The request's evidence accounted for (2026-10-29, after the third review of the graph experiment): every number,
date and stored value the earlier steps found in a request must be used by one of its parts, or the request is not
built (never run without one of its restrictions: "without a barcode that weigh more than 200 grams" ran without the
weight). Evidence is an occurrence in the request, never a value: two "0"s are two pieces of evidence.

    found(r, values, params, dates)  [(label, start, end)]: step 3's stored values, the numbers, the dates
    used(cond, group, formula, part, text)  [(start, end)]: the words the request's parts consumed (each condition's
                                     "words", the limit's, the formula's steps, a change's written value)
    shared(cond, text)               the words two conditions both used
    unaccounted(found, used)         the labels of the evidence no consumed words overlap (a duration around its
                                     number, the number inside a date: one occurrence read two ways, accounted for)
Positions are the request's own (step 3's are the prompt's: shifted by the request's start). An existence test's
implied count ("at least one black variant") covers no written 1 or 0: it would cover the 0 of another, dropped
condition (expert review, 2026-10-29); such a request is refused until the existence test's own words are tracked."""


def found(r: dict, values: list[dict], params: list[dict], dates: list[dict]) -> list[tuple[str, int, int]]:
    at = r.get("start", 0)
    out = [(f"the value '{v['text']}'", v["start"] - at, v["end"] - at) for v in values
           if v["request"] == r["id"] and v.get("role") == "lookup" and "start" in v]
    out += [(f"the number '{p['text']}'", p["start"], p["end"]) for p in params
            if p.get("value") is not None and "start" in p]
    return out + [(f"the date '{d['text']}'", d["start"], d["end"]) for d in dates if "start" in d]


def used(cond: dict, group: dict, formula: list[dict], part: dict, text: str) -> list[tuple[int, int]]:
    out = [tuple(w) for p in cond.get("predicates", []) for w in p.get("words", [])]
    out += [tuple(w) for w in cond.get("having_words", []) + group.get("limit_words", [])]
    out += [(x["start"], x["end"]) for x in formula if x.get("role") != "where"]   # a "where" step is a condition's
    for a in part.get("assignments", []):                     # a change's written value, as written
        v = (a.get("value") or {}).get("value") if isinstance(a.get("value"), dict) else None
        if isinstance(v, (str, int, float)) and (i := text.find(str(v))) >= 0:
            out.append((i, i + len(str(v))))
    return out


def shared(cond: dict, text: str) -> list[str]:
    """Words two conditions both used ("sold on or after October 4": the date's 4 read again as quantity = 4): one
    occurrence is one piece of evidence for one condition. Not a misreading: the same condition built twice (a
    duplicate), or the same text written twice ("created in September, sold in September"), its other occurrence
    used by no condition: that one is the second condition's."""
    import json
    preds = cond.get("predicates", [])
    taken = [tuple(w) for p in preds for w in p.get("words", [])]

    def twin(a, b):                                           # the same words elsewhere, used by no condition
        i = text.find(text[a:b])
        while i >= 0:
            if i != a and not any(s < i + (b - a) and i < e for s, e in taken):
                return True
            i = text.find(text[a:b], i + 1)
        return False
    out = set()
    for i, p in enumerate(preds):
        for q in preds[i + 1:]:
            if json.dumps(p.get("condition"), sort_keys=True) == json.dumps(q.get("condition"), sort_keys=True):
                continue
            for a, b in map(tuple, p.get("words", [])):
                for c, d in map(tuple, q.get("words", [])):
                    lo, hi = max(a, c), min(b, d)
                    if lo < hi and not twin(lo, hi):
                        out.add(f"'{text[lo:hi]}'")
    return sorted(out)


def unaccounted(found: list[tuple[str, int, int]], used: list[tuple[int, int]]) -> list[str]:
    return [label for label, a, b in found if not any(s < b and a < e for s, e in used)]
