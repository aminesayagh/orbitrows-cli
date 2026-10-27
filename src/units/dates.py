"""Dates in a request: where they are, what they are, read by datephrase (units/datephrase, over Duckling; written by
the user's date2num session, adopted 2026-10-27, moved into the CLI the same day). One reader for every step, as
units/quantity is for numbers. The reader reads a phrase with its classification and never guesses it, so:

    spans(text, gliner)   GLiNER2 cuts the phrases, without the words that compare with them ("the last 30 days" →
                          "30 days", "before October 5, 2026" → "October 5, 2026"): the caller's condition applies those
    questions(key, said, phrase)   Jev's classification of one phrase, asked in the caller's call: what it is (a day, a
                          whole period, a length of time, none), whether it is written in full or counts from today,
                          past or future; and the day/month order when the phrase has two real readings ("01/02/2026"),
                          the reader's only question, always the user's (never Jev's guess)
    read(phrase, answers, key)   the value: {"kind": "date", "date": iso} · {"kind": "period", "start", "end": iso,
                          end excluded} · {"kind": "duration", "quantity", "unit", "date": the session date moved by it,
                          the way the request says (past or future), "length": the characters of the phrase it read
                          ("one month" of "one month earlier")}; {"ask": the warning with its question} when an
                          answer is unsure; None when the phrase is no date or the reader can't read it

The session's date is the reference (TODAY, set by a caller that pins it, else the clock): passed on every read, so the
reader never falls back on its own clock. A number written in words in a length of time ("twenty-one days") is read by
units/quantity first: the reader's dateparser doesn't read number words.
"""
import re
from datetime import date

from dateutil.relativedelta import relativedelta
from .datephrase import EngineUnavailable, TemporalContext, answer, read_temporal

from orbitrows.services.model.client import SURE, choice
from .quantity import find, plain

DOWN = ("the date reader's service (Duckling) is not reachable: start it (docker run -d -p 127.0.0.1:8000:8000 "
        "rasa/duckling) or set DATEPHRASE_DUCKLING_URL")       # datephrase 0.3.0 reads every date with Duckling
TODAY: date | None = None              # the session's date (set as it starts), or a benchmark's; else the clock
LABEL = {"time": {"description": "a date, a calendar period or a length of time, without the words comparing with it "
                                 "or moving by it ('before', 'after', 'since', 'in the last', 'older than', 'within', "
                                 "'later', 'earlier'): 'October 5, 2026', 'yesterday', 'last month', 'September 2026', "
                                 "'August', '30 days', '3 months', 'one week', 'a year'", "threshold": 0.5}}
CONTEXT = "A request to read or change a store's database names a date, a period or a length of time."
KINDS = {"date": "One day ('October 5, 2026', 'yesterday', 'March 15').",
         "period": "A whole period, every day of it ('last month', 'September 2026', 'in August', 'this week').",
         "duration": "A length of time ('30 days' in 'the last 30 days', '3 months' in 'older than 3 months', '2 "
                     "weeks' in 'move it 2 weeks later').",
         "none": "Not a date or a time: a name, a code, a number."}
ANCHORS = {"absolute": "Written in full, with its year ('October 5, 2026', 'September 2026').",
           "relative": "It depends on today's date: 'yesterday', 'last month', or a month or a day without its year "
                       "('in August', 'March 15')."}
DIRECTIONS = {"past": "Before today: records that already happened ('the sales of August', 'the last 30 days', '3 "
                      "months ago').",
              "future": "After today ('next week', 'in 2 weeks', 'due in 10 days')."}
UNITS = {"day": "days", "week": "weeks", "month": "months", "year": "years"}


def today() -> date:
    return TODAY or date.today()


def spans(text: str, gliner) -> list[dict]:
    r = gliner.extract_entities(text, LABEL, include_confidence=True, include_spans=True)
    return sorted(({"text": s["text"], "start": s["start"], "end": s["end"]} for s in r.get("entities", {}).get(
        "time", [])), key=lambda s: s["start"])


def words_read(phrase: str) -> str:
    """"twenty-one days" → "21 days": the number words the shared reader reads, as digits."""
    for q in reversed(find(phrase)):
        if not q["text"][0].isdigit():
            phrase = phrase[:q["start"]] + str(plain(q["value"])) + phrase[q["end"]:]
    return phrase


def questions(key: str, said: str, phrase: str) -> dict:
    q = {f"{key}|kind": choice(CONTEXT, f"In {said}, what is '{phrase}'?", KINDS),
         f"{key}|anchor": choice(CONTEXT, f"In {said}, is '{phrase}' written in full, or does it depend on today's "
                                          f"date?", ANCHORS),
         f"{key}|direction": choice(CONTEXT, f"In {said}, is '{phrase}' before today or after today?", DIRECTIONS)}
    r = read_temporal(phrase, context=TemporalContext(reference_date=today()))
    if r.status == "ask":                                     # two real readings: the user's choice
        q[f"{key}|order"] = choice(CONTEXT, r.question.text, {o.value: o.label for o in r.question.options})
    return q


def read(phrase: str, answers: dict, key: str) -> dict | None:
    def pick(name):
        return answers.get(f"{key}|{name}", (None, {}))
    kind, kp = pick("kind")
    anchor, ap = pick("anchor")
    direction, dp = pick("direction")
    order, op = pick("order")
    unsure = [(n, v, p) for n, v, p in (("kind", kind, kp), ("anchor", anchor, ap), ("direction", direction, dp))
              if v is not None and p.get(v, 1) < SURE and (n == "kind" or kind != "none")
              and (n != "anchor" or kind != "duration") and (n != "direction" or kind == "duration" or anchor ==
                                                                                                      "relative")]
    if unsure:
        n, v, p = unsure[0]
        return {"ask": {"problem": f"what '{phrase}' is ({n}) uncertain", "answer": v, "probability": round(p[v], 2),
                        "key": f"{key}|{n}"}}
    if kind in (None, "none"):
        return None
    ctx = TemporalContext(reference_date=today())
    if kind == "duration":                                    # the longest start of the phrase read as one ("one month
        end = len(phrase)                                     # earlier": GLiNER2 may keep a word); never past a refusal
        r = read_temporal(words_read(phrase), context=ctx, kind="duration")   # with its reason ("two months and five
        if r.status != "resolved" and not r.reason:          # days": MIXED_UNITS), never dropping a number or a
            for end in reversed([m.end() for m in re.finditer(r"\S+", phrase)][:-1]):   # length ("plus a week")
                tail = phrase[end:]
                if find(tail) or read_temporal(words_read(tail), context=ctx, kind="duration").status == "resolved":
                    break
                if (r := read_temporal(words_read(phrase[:end]), context=ctx, kind="duration")).status == "resolved":
                    break
        if r.status != "resolved" or r.value.unit not in UNITS:
            return None
        n = r.value.quantity * (-1 if direction != "future" else 1)
        whole = float(n).is_integer() or r.value.unit in ("day", "week")    # half a month has no calendar date
        moved = today() + relativedelta(**{UNITS[r.value.unit]: n}) if whole else None
        return {"kind": "duration", "quantity": r.value.quantity, "unit": UNITS[r.value.unit], "length": end,
                **({"date": moved.isoformat()} if moved else {})}
    rel = anchor == "relative"
    r = read_temporal(phrase, context=ctx, kind=kind, anchor="relative" if rel else "absolute",
                      direction=(direction or "past") if rel else None)
    if r.status == "ask":
        if order is None or op.get(order, 0) < 1:              # only the user's answer (settled: certain)
            return {"ask": {"problem": r.question.text, "answer": order, "probability": round(op.get(order, 0), 2),
                            "key": f"{key}|order"}}
        r = answer(r, order)
    if r.status != "resolved":
        return None
    v = r.value
    if kind == "period":
        return {"kind": "period", "start": v.start.isoformat(), "end": v.end.isoformat()}
    return {"kind": "date", "date": v.date.isoformat()}
