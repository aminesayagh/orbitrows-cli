"""The helpers of the formula's pass 1 (operations_jev.py): the request's words as spaCy reads them, its number
occurrences, its segments (one per step of a change) and the words naming columns; Record, one operation step.
The PoC's option A (2026-10-14: a change read by word rules, no Jev) lived here too; option B (Jev) replaced it in
the pipeline and its rules were never called: removed 2026-10-25.
"""
import functools
import re

from pydantic import BaseModel
from text_to_num import text2num

WORD = re.compile(r"[^\W\d_]+(?:['’-][^\W\d_]+)*")

PERCENT = r"\s*(?:%|per\s?cent\b|percent\b|pour\s?cent\b|pourcent\b)"

FACTOR_VERBS = {r"halv(?:e|es|ed|ing)|in\s+half|by\s+half": 0.5, r"doubl(?:e|es|ed|ing)": 2, r"tripl(?:e|es|ed|ing)": 3,
                r"quadrupl(?:e|es|ed|ing)": 4}                  # the verb is the operation: "halve the cost"

FACTORS = {"twice": 2, "half": 0.5, "a third": 0.333333, "one third": 0.333333,
           "a quarter": 0.25, "one quarter": 0.25, "deux fois": 2, "le double": 2, "la moitié": 0.5, "moitié": 0.5,
           "un tiers": 0.333333, "un quart": 0.25}


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
