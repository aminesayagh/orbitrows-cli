"""header_context: the unit, currency or language a column header declares ("Poids (kg)" -> kg).

GLiNER2 finds labelled spans in the header ("kilos" as a measurement unit, "fr" as a language); the dictionary
turns each span into a standard unit, looking only at meanings of the span's category (CLDR unit names in every
locale checked against Pint, Babel currencies and languages). Chosen over a tokenizer after the benchmark in
poc/docs/header-context-benchmark.md.

Ambiguity is never guessed: a symbol shared by several currencies stays as written ("$"), a name with several
meanings resolves to nothing, a compound resolves completely or not at all, and independent spans that disagree
resolve to nothing.
"""

import difflib
import re
import unicodedata
from functools import cache

MODEL = "fastino/gliner2-multi-v1"  # gliner2.5-multi-v1 tested: 77% vs 92% on the benchmark
# Locales whose currency and language NAMES are read ("euros", "français"); None = every Babel locale.
NAME_LOCALES = ("en", "fr", "de", "es", "it", "nl", "pt")
THRESHOLD = 0.5  # the library default, as benchmarked
# GLiNER2 entity labels: the parts a header declares.
LABELS = {
    "currency": "A currency code, symbol or name that the column's prices are expressed in, e.g. EUR, €, $, dirham.",
    "measurement unit": "A unit of weight, length or volume the column's values are expressed in, e.g. kg, litres, cm.",
    "price per unit": "A currency per unit of measure, e.g. €/kg, EUR/L.",
    "language": "A language name or code the column's text is written in, e.g. fr, English.",
    "percentage": "A percent sign or percentage scale, e.g. %.",
}
# Only with CLASSIFY_RELATION: the quantity a rate is per ("pour 100 g").
QUANTITY_LABEL = {
    "unit quantity": "A number stating how much of a unit an amount refers to, such as the 100 in 'per 100 g'. "
                     "Not a price, a code or an identifier.",
}
# GLiNER2 classification of the header when it holds several declarations: how are they related?
RELATIONS = {
    "rate": "A rate: an amount priced or measured per one unit or quantity of something, such as euros per kilo.",
    "multiply": "A product of two units multiplied together into one unit, such as kilowatt hours or metres times metres.",
    "independent": "Two separate labels or declarations, each giving its own unit, not combined into one expression.",
    "unclear": "Units written next to each other with nothing saying how they relate.",
}
CLASSIFY_RELATION = False  # switch on once the benchmark validates it (doc/to-review.md)
LABEL_KIND = {"currency": "currency", "measurement unit": "physical_unit", "price per unit": "compound_unit",
              "language": "language", "percentage": "percentage"}
# CLDR unit category -> the Pint dimension its units must have ("volume-dram" is not Pint's dram, a mass).
DIMENSION_OF = {"mass": "g", "length": "m", "area": "m**2", "volume": "l"}
# CLDR builds area and volume ids from a base unit ("area-square-meter", "volume-cubic-centimeter").
CLDR_POWERS = (("square-", 2), ("cubic-", 3))
QUANTITY = re.compile(r"^(\d+(?:[.,]\d+)?)?\s*(.*)$")  # "100 g" -> ("100", "g"); "kg" -> (None, "kg"); "100" -> ("100", "")
EXPONENT = re.compile(r"^(.+?)\s*(?:(?:\*\*|\^)\s*(\d)|([²³]))$")  # "m²", "m**2", "m^2"; not "m2" (a code?)


def fold(text: str) -> str:
    """Casefold, apply NFKD compatibility forms and drop accents: "Mètre" -> "metre", "m²" -> "m2".

    The micro sign becomes Greek mu ("µg" -> "μg"), which is kept: it is not "g".
    """
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)).casefold()


@cache
def _active_codes() -> set[str]:
    """Currencies in use today in some territory: lowercase "sur" must not become the Soviet rouble."""
    from babel import Locale
    from babel.numbers import get_territory_currencies

    return {c for t in Locale("en").territories if len(t) == 2 for c in get_territory_currencies(t)}


def _cldr_unit(ureg, name: str):
    """A CLDR unit name as a Pint unit: "square-meter" -> m ** 2 (Pint has no square_meter), "foot" -> ft."""
    for prefix, power in CLDR_POWERS:
        if name.startswith(prefix):
            return ureg.Unit(name[len(prefix):].replace("-", "_")) ** power
    return ureg.Unit(name.replace("-", "_"))


@cache
def _tables():
    """Built once, on first use: the CLDR scan takes a moment. Every table keeps all meanings, never a first winner."""
    import pint
    from babel import Locale, localedata
    from babel.numbers import get_currency_name, get_currency_symbol
    from babel.units import get_unit_name

    ureg = pint.UnitRegistry()
    locales = localedata.locale_identifiers()

    codes, symbols = set(), {}  # symbol -> every currency code it stands for in some locale
    for loc in ["en", *locales]:
        for code in Locale.parse(loc).currencies:
            codes.add(code)
            symbol = get_currency_symbol(code, loc)
            if symbol and symbol != code and (len(symbol) > 1 or not symbol.isalpha()):
                symbols.setdefault(symbol, set()).add(code)

    physical, skipped = {}, []  # CLDR unit id -> Pint symbol, only when Pint reads the same dimension
    for unit_id in Locale("en")._data["unit_display_names"]:
        category, _, name = unit_id.partition("-")
        if category not in DIMENSION_OF:
            continue
        try:
            unit = _cldr_unit(ureg, name)
        except (pint.UndefinedUnitError, ValueError, AttributeError) as e:
            skipped.append((unit_id, type(e).__name__))
            continue
        if unit.dimensionality != ureg.Unit(DIMENSION_OF[category]).dimensionality:
            skipped.append((unit_id, f"Pint reads {unit} ({unit.dimensionality})"))
            continue
        physical[unit_id] = f"{unit:~}"

    names = {}  # folded name (4+ letters) -> every (kind, unit) it has in some locale
    for loc in locales:
        for unit_id, symbol in physical.items():
            for length in ("long", "short", "narrow"):
                try:
                    name = fold(get_unit_name(unit_id, length=length, locale=loc) or "").strip(".")
                except (KeyError, ValueError):
                    continue
                if len(name) >= 4:
                    names.setdefault(name, set()).add(("physical_unit", symbol))
    for loc in NAME_LOCALES or locales:
        locale = Locale.parse(loc)
        for code in locale.currencies:
            for count in (1, 2):
                names.setdefault(fold(get_currency_name(code, count, loc)), set()).add(("currency", code))
        for code, name in locale.languages.items():
            names.setdefault(fold(name), set()).add(("language", code))
    physical_names = [n for n, meanings in names.items() if any(k == "physical_unit" for k, _ in meanings)]
    language_codes = {code for code in Locale.parse("en").languages if len(code) == 2}
    dimensions = {ureg.Unit(u).dimensionality for u in DIMENSION_OF.values()}
    return ureg, dimensions, codes, symbols, names, physical_names, language_codes, skipped


def skipped_units() -> list[tuple[str, str]]:
    """CLDR units left out of the dictionary, with the reason (unknown to Pint, or another dimension)."""
    return _tables()[-1]


def _unique(meanings: set[tuple[str, str]], kind: str | None) -> dict | None:
    """The one meaning of the wanted kind, or None when there is none or several."""
    found = {m for m in meanings if kind in (None, m[0])}
    if len(found) != 1:
        return None
    (k, unit), = found
    return {"kind": k, "unit": unit}


def _physical(token: str) -> dict | None:
    """A physical unit, possibly with an explicit exponent ("m²" -> "m ** 2"): the base unit is checked, then raised."""
    if match := EXPONENT.match(token):
        base = _physical(match[1])
        return base and _raise(base, int(match[2] or unicodedata.digit(match[3])))
    ureg, dimensions, _, _, names, *_ = _tables()
    # Pint first, exact case ("ml" millilitre, "Ml" megalitre, "g" gram, not µg), then "KG"/"Kg", then plurals ("kgs")
    for candidate in dict.fromkeys([token, token.lower(), token[:-1] if len(token) > 2 and token.endswith("s") else ""]):
        try:
            if candidate and candidate in ureg and ureg.Unit(candidate).dimensionality in dimensions:
                return {"kind": "physical_unit", "unit": f"{ureg.Unit(candidate):~}"}
        except (ValueError, AttributeError):  # Pint rejects some strings it claims to contain
            pass
    word = fold(token).strip(".")
    if len(word) < 4:  # 3-letter names collide across languages ("vat" is a barrel in Dutch)
        return None
    found = _unique(names.get(word, set()), "physical_unit")
    if not found and word.endswith("s"):  # plural: "kilos", "litres"
        found = _unique(names.get(word[:-1], set()), "physical_unit")
    return found


def _raise(unit: dict, power: int) -> dict:
    ureg = _tables()[0]
    return {"kind": "physical_unit", "unit": f"{ureg.Unit(unit['unit']) ** power:~}"} if power != 1 else unit


def _compound(token: str) -> dict | None:
    """A price per unit: currency / [quantity] physical unit ("EUR/kg", "€/100g"). Complete, or None."""
    left, _, right = token.partition("/")
    currency = lookup(left, "currency")
    match = QUANTITY.match(right.strip())
    unit = match[2].strip() and _physical(match[2].strip())
    if not (currency and unit):
        return None
    quantity = match[1] or ""
    return {"kind": "compound_unit", "unit": f"{currency['unit']}/{quantity}{unit['unit']}"}


def lookup(token: str, kind: str | None = None) -> dict | None:
    """One span or token -> {"kind": a context.CATEGORIES name, "unit"}, or None.

    `kind` restricts which meanings are considered (GLiNER2's label, or Jev's category for a "both" column):
    "pt" labelled as a language is Portuguese, never a pint.
    """
    _, _, codes, symbols, names, physical_names, language_codes, _ = _tables()
    wants = lambda k: kind in (None, k)  # noqa: E731
    token = token.strip(".,;:()[]")
    if not token:
        return None
    if "/" in token:  # a currency over a unit is a price per unit, even when the span was labelled currency
        return _compound(token) if kind in (None, "compound_unit", "currency") else None
    if wants("currency"):
        if token in codes:
            return {"kind": "currency", "unit": token}
        if token in symbols and not token.isalpha():
            shared = symbols[token]  # "$" is USD, CAD, AUD…: keep the symbol rather than guess
            return {"kind": "currency", "unit": next(iter(shared)) if len(shared) == 1 else token}
    if wants("percentage") and token in ("%", "‰"):
        return {"kind": "percentage", "unit": token}
    if wants("physical_unit") and (found := _physical(token)):
        return found
    if wants("currency") and len(token) == 3 and token.upper() in _active_codes():  # "eur", "mad"
        return {"kind": "currency", "unit": token.upper()}
    if wants("language") and len(token) == 2 and token.casefold() in language_codes:
        return {"kind": "language", "unit": token.casefold()}
    word = fold(token)
    if len(word) >= 4 and (found := _unique(names.get(word, set()), kind)):
        return found
    if len(word) >= 5 and wants("physical_unit"):  # typos like "litres"; "incl" must not fuzzy-match "inch"
        close = difflib.get_close_matches(word, physical_names, n=2, cutoff=0.85)
        units = {u for n in close for k, u in names[n] if k == "physical_unit"}
        if len(units) == 1:  # the close names agree on one unit
            return {"kind": "physical_unit", "unit": units.pop()}
    return None


def _resolve_span(text: str, kind: str | None) -> dict | None:
    """A span resolves as a whole ("mètres carrés", "€/kg"), never from one of its words: "mètres" alone
    would turn a square metre into a metre."""
    span = text.strip(" ()[]")
    if "/" in span:
        return lookup(span.replace(" ", ""), kind)
    for candidate in dict.fromkeys([span, span.replace(" ", "")]):  # "100 g" and "100g" are the same unit
        if found := lookup(candidate, kind):
            return found
    return None


def _covers(compound: tuple[str, str], other: tuple[str, str]) -> bool:
    """A currency or unit that is part of a compound agrees with it ("EUR" and "EUR/kg")."""
    numerator, _, denominator = compound[1].partition("/")
    return compound[0] == "compound_unit" and other[1] in (numerator, denominator, QUANTITY.match(denominator)[2])


@cache
def _model():
    """Loaded once, on first use (seconds on CPU; the model is 1.2 GB, downloaded on the very first run)."""
    from gliner2 import GLiNER2

    return GLiNER2.from_pretrained(MODEL)


def warm_up() -> None:
    """Build the dictionary and load the model; call off the UI thread."""
    _tables()
    _model()


def _spans(header: str) -> list[tuple[int, int, float, str, str]]:
    """GLiNER2 spans (start, end, confidence, label, text) in reading order; overlapping ones keep the longest,
    then the surest ("€/kg" over "€" and "kg")."""
    out = _model().extract_entities(
        header.replace("_", " "), LABELS | (QUANTITY_LABEL if CLASSIFY_RELATION else {}), threshold=THRESHOLD,
        include_confidence=True, include_spans=True,
    )
    spans = sorted(
        ((e["start"], e["end"], e["confidence"], label, e["text"]) for label, items in out["entities"].items()
         for e in items),
        key=lambda s: (s[1] - s[0], s[2]), reverse=True,
    )
    kept = []
    for span in spans:
        if all(span[1] <= k[0] or span[0] >= k[1] for k in kept):
            kept.append(span)
    return sorted(kept)


def spans(header: str) -> list[tuple[str, str, int, int]]:
    """GLiNER2's kept spans for a header, as (label, text, start, end), for benchmarks and debugging."""
    return [(label, text, start, end) for start, end, _, label, text in _spans(header)]


@cache
def _relation_schema():
    return _model().create_schema().classification("relation", RELATIONS, multi_label=False)


def relation(header: str) -> tuple[str, float]:
    """GLiNER2's reading of how a header's declarations relate: (a RELATIONS label, confidence)."""
    out = _model().extract(header.replace("_", " "), _relation_schema(), include_confidence=True)["relation"]
    return out["label"], out["confidence"]


def _number(text: str) -> str | None:
    """A written quantity ("100", "0,5" -> "0.5"); words like "six" stay unresolved."""
    return text.replace(",", ".") if re.fullmatch(r"\d+(?:[.,]\d+)?", text) else None


def _parts(spans, category: str | None) -> list[tuple[str, str]]:
    """Declarations in reading order, as (kind, unit), with "qty" for a quantity GLiNER2 found and "?" for a
    span the dictionary can't read (the "DH" of "Prix (DH/kg)", or a subject word like "Preis")."""
    parts = []
    for _, _, _, label, text in spans:
        text = text.strip(" ()[]")
        if label == "unit quantity":
            if quantity := _number(text):
                parts.append(("qty", quantity))
        elif found := _resolve_span(text, category or LABEL_KIND[label]):
            parts.append((found["kind"], found["unit"]))
        elif not category:  # with a category, spans of other categories are expected to be left out
            parts.append(("?", text))
    return parts


def _combine(parts: list[tuple[str, str]], how: str) -> tuple[str, str] | None:
    """One declaration from several, as GLiNER2 read their relationship; None when they don't fit it.

    rate: a currency per [quantity] physical unit ("EUR/100g"): the currency is the numerator, since a price
    column is priced per unit. multiply: two physical units ("m ** 2").
    """
    decls = [p for p in parts if p[0] != "qty"]
    if how == "rate":
        currencies = [u for k, u in decls if k == "currency"]
        units = [i for i, (k, _) in enumerate(parts) if k == "physical_unit"]
        if len(currencies) != 1 or len(units) != 1 or len(decls) != 2:
            return None
        i = units[0]
        quantity = parts[i - 1][1] if i > 0 and parts[i - 1][0] == "qty" else ""
        return "compound_unit", f"{currencies[0]}/{quantity}{parts[i][1]}"
    if how == "multiply":
        units = [u for k, u in decls if k == "physical_unit"]
        if len(units) != 2 or len(decls) != 2:
            return None
        ureg = _tables()[0]
        return "physical_unit", f"{ureg.Unit(units[0]) * ureg.Unit(units[1]):~}"
    return None


def resolve_header(header: str, category: str | None = None) -> dict | None:
    """The context a header declares: {"category": a context.CATEGORIES name, "unit": "kg"}, or None.

    Each span is resolved within its own category (Jev's `category` wins for a "both" column). When several
    declarations remain, GLiNER2 classifies their relationship (with CLASSIFY_RELATION): a rate or a product
    is combined into one; independent ones must agree; an unclear one resolves to nothing. Slow: call off the
    UI thread.
    """
    parts = _parts(_spans(header), category)
    unread = any(k == "?" for k, _ in parts)
    parts = [p for p in parts if p[0] != "?"]
    listed = [p for p in parts if p[0] != "qty" and not any(o != p and _covers(o, p) for o in parts)]
    decls = set(listed)
    if CLASSIFY_RELATION and (len(listed) > 1 or (unread and listed)):  # "(m) x (m)" is two declarations
        how, _ = relation(header)
        if unread and how in ("rate", "multiply"):
            return None  # one combined expression with a part we can't read: never answer from the rest
        combined = _combine(parts, how) if how in ("rate", "multiply") else None
        if combined or how == "unclear":
            decls = {combined} if combined else set()
    if category:
        decls = {d for d in decls if d[0] == category}
    if len(decls) != 1:  # nothing found, or independent declarations that disagree
        return None
    (kind, unit), = decls
    return {"category": kind, "unit": unit}
