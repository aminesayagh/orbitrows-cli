"""The request's evidence accounted for (2026-10-29, after the third review of the graph experiment): every number,
date and stored value the earlier steps found in a request must be used by one of its parts, or the request is not
built (never run without one of its restrictions: "without a barcode that weigh more than 200 grams" ran without the
weight). Evidence is an occurrence in the request, never a value: two "0"s are two pieces of evidence.

    found(r, values, params, dates)  [(label, start, end)]: step 3's stored values, the numbers, the dates
    used(cond, group, formula, part, text)  [(start, end)]: the words the request's parts consumed (each condition's
                                     "words", the limit's, the formula's steps, a change's written value)
    implied(cond, params)            the numbers a test of related records means: 1 for an existence, 0 for an absence
    unaccounted(found, used)         the labels of the evidence no consumed words overlap (a duration around its
                                     number, the number inside a date: one occurrence read two ways, accounted for)
Positions are the request's own (step 3's are the prompt's: shifted by the request's start)."""


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


def implied(cond: dict, params: list[dict]) -> list[tuple[int, int]]:
    """The numbers a test of related records means without writing them: an existence test is a count of at least 1
    ("at least one black variant"), an absence a count of 0 ("with no orders"): those numbers are its own."""
    kinds = {p.get("operator") for p in cond.get("predicates", []) if p.get("linked")}
    means = ({1} if "in" in kinds else set()) | ({0} if "not_in" in kinds else set())
    return [(p["start"], p["end"]) for p in params if "start" in p and p.get("value") in means]


def unaccounted(found: list[tuple[str, int, int]], used: list[tuple[int, int]]) -> list[str]:
    return [label for label, a, b in found if not any(s < b and a < e for s, e in used)]
