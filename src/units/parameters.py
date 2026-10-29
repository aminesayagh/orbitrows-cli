"""Parameters of a request: every number it writes, with its value and unit ("by 10%", "750 grams", "€ 20").
One algorithm, no Jev (decided 2026-10-11; benchmark v2/bench/parameters):

    1. text   GLiNER2 finds the parameters' spans (two labels: an amount, a number in words); it decides which
              numbers are parameters ("ORD-1042", "the A5 notebook" are not)
    2. value  the number inside the span, read by the import's cell reader (dictionary.to_number: "1 189,00",
              "12,5", "1,250.50"); a number written in words has no value yet (no reader installed: a known gap)
    3. unit   import algorithm 4's rule: only the token right next to the number counts, just after it ("750 g",
              "500ml", "10 %") or a symbol / code just before it ("€20", "USD 45"), resolved by the tested header
              dictionary (header.lookup: CLDR names in every locale, Pint, Babel). One or two words after the
              number are tried ("pour cent"); a word the dictionary doesn't know is no unit, never a guess

    parameters("Lower the price by 10%, never below 30 €", model)
      → [{"text": "10%", "start": 23, "end": 26, "value": 10, "unit": {"kind": "percentage", "unit": "%"}}, …]
"""
import re

from babel.numbers import get_currency_symbol

from .dictionary import NUMBER, PIECE, plain, to_number
from .header import currencies, lookup
from .quantity import find, plain as as_number
from .quantity_parser.en import NONE_WORDS

LABELS = {
    "amount": "a number with its unit, currency or percent sign when one is written: '10%', '750 grams', '20 €', "
              "'3 days'",
    "number word": "a number written in words, with its unit if any: 'ten', 'two weeks', 'dix pour cent', 'a dozen'",
}
THRESHOLD = 0.5                                               # the library default, as in the header benchmark
NUM = re.compile(NUMBER)
AFTER = re.compile(r"\s*([^\W\d_][\w]*|[^\w\s.,;:!?()\[\]'\"]{1,2})(?:\s+([^\W\d_][\w]*))?")    # "g", "%", "pour cent"
BEFORE = re.compile(r"([^\W\d_]{1,3}|[^\w\s.,;:!?()\[\]'\"]{1,2})\s*$")                           # "€", "USD", "$"


def spans(text: str, model) -> list[dict]:
    """GLiNER2's parameter spans, overlapping ones kept once (the longest)."""
    found = []
    for label, description in LABELS.items():
        r = model.extract_entities(text, {label: {"description": description, "threshold": THRESHOLD}},
                                   include_confidence=True, include_spans=True)
        found += r.get("entities", {}).get(label, [])
    kept = []
    for s in sorted(found, key=lambda s: (-(s["end"] - s["start"]), -s["confidence"])):
        if all(s["end"] <= k["start"] or s["start"] >= k["end"] for k in kept):
            kept.append(s)
    return sorted(kept, key=lambda s: s["start"])


def currency(token: str, store: str | None) -> dict | None:
    """A currency several ones share ("$", "dollars", "dirhams"): the store's currency when it is one of them, else the
    one CLDR English writes with the bare symbol ($ → USD, £ → GBP), marked assumed (docs/algorithms.md, algorithm 4);
    else none."""
    codes = currencies(token)
    if store in codes:
        return {"kind": "currency", "unit": store}
    plain_symbol = [c for c in codes if not any(ch.isalpha() for ch in get_currency_symbol(c, "en"))]   # $, not A$
    return {"kind": "currency", "unit": plain_symbol[0], "assumed": True} if len(plain_symbol) == 1 else None


def resolve(token: str, store: str | None = None) -> dict | None:
    """One token next to a number → its unit: the header dictionary, then a shared currency, then a piece."""
    u = lookup(token)
    if u and u["kind"] == "language":
        u = None
    if u and u["kind"] == "currency" and u["unit"] == token.strip(".,;:") and len(currencies(token)) > 1:
        u = currency(token, store) or u                       # "$" kept as written when nothing decides
    if u is None and len(plain(token)) >= 4 and currencies(token):
        u = currency(token, store)
    if u is None and plain(token).strip(".") in PIECE:        # the dictionary has no counting units (dictionary.py)
        u = {"kind": "counting_unit", "unit": "pc"}
    return u


def words_value(text: str, start: int, end: int) -> tuple[float | None, int]:
    """A number written in words inside text[start:end] ("twenty-five percent", "twice", "a third"): the shared reader
    (units/quantity) on the span; (value, where the number ends) or (None, start). A percentage's word stays outside
    (its unit, read next: "twenty-five" → 25, "percent" → %)."""
    for q in find(text[start:end]):
        if q["kind"] in ("number", "multiplier", "percentage"):
            if q["kind"] == "percentage":
                bare = re.sub(r"\s*(?:%|per\s?cent|percent)$", "", q["text"], flags=re.I)
                return as_number(q["value"] * 100), start + q["start"] + len(bare)
            return as_number(q["value"]), start + q["end"]
    return None, start


def unit_at(text: str, start: int, end: int, store: str | None = None) -> tuple[dict | None, int, int]:
    """The unit written right next to the number text[start:end], and the parameter's new bounds.
    Before a number only a currency counts: a symbol ("$15": it wins over a word after, "in the US") or a code in
    capitals ("USD 45"; "top 5" is not the Tongan TOP)."""
    b = BEFORE.search(text, 0, start)
    before = None
    if b and (not b.group(1).isalpha() or b.group(1).isupper()):
        before = resolve(b.group(1), store)
        before = before if before and before["kind"] == "currency" else None
    if before and not b.group(1).isalpha():
        return before, b.start(1), end
    m = AFTER.match(text, end)
    if m:
        two = m.group(2) and lookup(f"{m.group(1)} {m.group(2)}")
        if two:
            return two, start, m.end(2)
        if one := resolve(m.group(1), store):
            return one, start, m.end(1)
    if before:
        return before, b.start(1), end
    return None, start, end


def glued(text: str, start: int, end: int, unit: dict | None) -> bool:
    """A number stuck to a code: letters or a joining sign before it ("A5", "ORD-1042"; a minus after a space is a
    sign: "-15%"), or letters after it that are not its unit ("6y", "15b"; "500ml" is a unit)."""
    before = start > 0 and (text[start - 1].isalpha() or text[start - 1] in "_/#" or
                            (text[start - 1] == "-" and start > 1 and text[start - 2].isalnum()))
    after = end < len(text) and text[end].isalpha() and unit is None
    return before or after


def parameters(text: str, model, store: str | None = None) -> list[dict]:
    """store: the store's currency (settings), which decides a currency several ones share."""
    out = []
    for s in spans(text, model):
        # the number is read from the prompt where the span is: GLiNER2 may cut "1 250" in two
        nums = [m for m in NUM.finditer(text) if m.start() < s["end"] and s["start"] < m.end()]
        if not nums:                                          # a number in words
            value, b = words_value(text, s["start"], s["end"])
            unit, _, b2 = unit_at(text, s["start"], b, store) if value is not None else (None, 0, s["end"])
            out.append({"text": text[s["start"]:max(b2, s["end"])], "start": s["start"], "end": max(b2, s["end"]),
                        "value": value, "unit": unit, "words": True})
            continue
        for m in nums:                                        # "between 100 and 200": one span, two numbers
            a, b = m.start(), m.end()
            if any(o["start"] <= a < o["end"] for o in out):  # a number two spans share is read once
                continue
            unit, a2, b2 = unit_at(text, a, b, store)
            if glued(text, a, b, unit):
                continue
            out.append({"text": text[a2:b2], "start": a2, "end": b2, "value": to_number(m.group(0)), "unit": unit})
    for q in find(text):                                      # a quantity of none, by the reader's lexicon ("no stock",
        if q["text"].casefold() in NONE_WORDS and not any(    # "nothing reserved"): 0, which GLiNER2 never proposes;
                o["start"] < q["end"] and q["start"] < o["end"] for o in out):   # last, so the others keep their order
            out.append({"text": q["text"], "start": q["start"], "end": q["end"], "value": 0, "unit": None, "none": True})
    return out
