"""Numbers in a request: where they are, and what they are, read by quantity_parser (units/quantity_parser, written
by the text2num session in ../text_to_number, moved into this repository 2026-10-27; adopted 2026-10-26: 44/52
development and 19/21 held-out cases against 35/52 and 15/21 for Recognizers-Text, every fraction exact; vault
02_projects/pilotstore/quantity-normalizer-delivery-plan). One reader for every step: the formula's numbers, step 5's
operands, a read's limit, the parameters' values. The parser reads a phrase strictly; this module
finds the phrases: from each digit or number word (the parser's own lexicon), the longest window the parser reads
whole. Whether a number found here belongs to the request (the "one" of "one of the products") stays the caller's,
or Jev's, to decide: the reader only reads.

    find(text)  [{"text", "start", "end", "value": Fraction, "kind": number | multiplier | percentage | ordinal,
                  (the lexicon's words of none read 0: "no stock", "nothing reserved")
                  "form": integer | decimal | fraction | mixed_number | ordinal}] in text order, never overlapping
    read(text, expected_kind)  one phrase: its ParseResult (the caller's own span)
    plain(value)  a Fraction for SQL: an int when whole, else a float (the database boundary, as the plan says)
"""
import functools
import re
from fractions import Fraction

from .quantity_parser import parse_quantity
from .quantity_parser.en import CARDINALS, DENOMINATORS, MULTIPLIERS, NONE_WORDS, ORDINAL_TO_CARDINAL

TOKEN = re.compile(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[xX](?![^\W\d_]))?%?|[^\W\d_]+(?:-[^\W\d_]+)*|%|/")
# a digit token takes a comma only inside an en-US thousands group ("1,500", never the "5," of "nearest 5, never")
STARTS = {*CARDINALS, *DENOMINATORS, *MULTIPLIERS, *ORDINAL_TO_CARDINAL, *NONE_WORDS, "a", "an", "minus"}
WIDEST = 7                                                    # "one hundred and twenty five percent": words at most



@functools.lru_cache(maxsize=8192)
def read(text: str, expected_kind: str | None = None):
    return parse_quantity(text, expected_kind=expected_kind)


def starts(token: str) -> bool:
    w = token.casefold()
    return token[0].isdigit() or w in STARTS or (w.split("-")[0] in STARTS if "-" in w else False)


def find(text: str) -> list[dict]:
    toks = list(TOKEN.finditer(text))
    out, i = [], 0
    while i < len(toks):
        if not starts(toks[i].group(0)):
            i += 1
            continue
        for j in range(min(len(toks), i + WIDEST), i, -1):     # the longest window read whole
            a, b = toks[i].start(), toks[j - 1].end()
            r = read(text[a:b])
            if r.status == "resolved" and r.span == (0, b - a):
                q = r.quantity
                out.append({"text": text[a:b], "start": a, "end": b, "value": q.value, "kind": q.kind,
                            "form": q.form})
                i = j
                break
        else:
            i += 1
    return out


def plain(value: Fraction):
    return int(value) if value.denominator == 1 else float(value)
