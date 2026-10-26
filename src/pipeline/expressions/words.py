"""The helpers of the formula's pass 1 (steps.py): the request's words as spaCy reads them, its number
occurrences, its segments (one per step of a change) and the words naming columns; Record, one operation step.
The PoC's option A (2026-10-14: a change read by word rules, no Jev) lived here too; option B (Jev) replaced it in
the pipeline and its rules were never called: removed 2026-10-25.
"""
import functools
import re

from pydantic import BaseModel

from orbitrows.units.quantity import find, plain

WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")


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


def occurrences(text: str, lang: str, skip: list[tuple[int, int]]) -> list[dict]:
    """Every number written in the text, read by the shared reader (units/quantity: digits, number words, fractions,
    factors, percentages, exact); numbers inside a condition phrase are the condition's (skip). Whether a number is
    part of the change is Jev's (pass 1's "none"), never a word rule. A percentage keeps its number with "percent" set
    (15, not 3/20); kinds, from the reader's: digits · words · factor (a fraction in words: "half", "a third") ·
    factor_verb (a multiplier: "double", "twice", "halve", "2.5x"); an ordinal ranks, it is no amount."""
    out = []
    for q in find(text):
        if q["kind"] == "ordinal":
            continue
        pct, written = q["kind"] == "percentage", q["text"][:1].isdigit() or q["text"].startswith("-")
        kind = "factor_verb" if q["kind"] == "multiplier" else \
            "factor" if not written and q["form"] in ("fraction", "mixed_number") else "digits" if written else "words"
        out.append({"text": q["text"], "start": q["start"], "end": q["end"],
                    "value": plain(q["value"] * 100 if pct else q["value"]), "percent": pct, "kind": kind})
    out = [o for o in out if not any(s <= o["start"] < e for s, e in skip)]
    for i, o in enumerate(out, 1):
        o["occ"] = f"n{i}"
    return out


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
