"""Numbers and the unit written next to them in a cell ("16,50 €", "350 ml"). Units are read with the
header resolver's dictionary (src/units/header.py).

    cell_unit("16,50 €") → (16.5, {"kind": "currency", "unit": "EUR"})     cell_unit("extra 1 L") → None
"""
import re
import unicodedata

from .header import lookup, unit_name

NUMBER = r"[+-]?(?:\d{1,3}(?:[ .,\u00a0\u202f']\d{3})+|\d+)(?:[.,]\d+)?"
CELL = re.compile(rf"^\s*(?P<pre>[^\W\d_]{{1,3}}|[^\w\s])?\s*(?P<num>{NUMBER})\s*(?P<post>[^\W\d_][\w.]*|[^\w\s]{{1,2}}|[^\W\d_]+/[^\W\d_]+)?(?:\s+[^\W\d_][\w.]*)?\s*$")
# a word after the unit is allowed ("419,00 € TTC"); words before the number are not ("extra 1 L")


def plain(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()


def to_number(text: str):
    """"1 189,00" → 1189.0, "12,5" → 12.5, "1,234.50" → 1234.5. The last . or , followed by 1–2 digits, or by 4+,
    is the decimal mark; a single separator followed by exactly 3 digits is read as thousands."""
    t = re.sub(r"[   ']", "", text)
    seps = [c for c in t if c in ".,"]
    if not seps:
        return float(t)
    last = max(t.rfind("."), t.rfind(","))
    decimals = len(t) - last - 1
    if len(seps) == 1 and decimals == 3:
        return float(t.replace(",", "").replace(".", ""))
    whole, frac = t[:last], t[last + 1:]
    return float(whole.replace(",", "").replace(".", "") + "." + frac)


def cell_unit(cell: str):
    """(number, unit) when the whole cell is a number with a unit next to it ("16,50 €", "€ 16.50", "350 ml",
    "12,5 %"); (number, None) for a bare number; None when the cell holds anything else ("extra 1 L", "pack of 6")."""
    m = CELL.match(cell or "")
    if not m:
        return None
    pre, post = m.group("pre"), m.group("post")
    if pre and post:
        return None
    unit = None
    if pre or post:
        unit = lookup(pre or post)
        if unit is None or (pre and unit["kind"] not in ("currency",)):
            return None
    return to_number(m.group("num")), unit


def _try(token, kind=None):
    try:
        return lookup(token, kind)
    except Exception:                                         # Pint fails on some strings it claims to contain
        return None


def _lookup(token):
    """Only a token written exactly as its standard symbol or code ("GB", "kg", "l", "EUR"): never a name, a plural or
    a language, which are GLiNER2's job ("kilos" → ks, "DH" → decihour would be wrong)."""
    u = _try(token)
    return u if u and u["kind"] != "language" and u["unit"] == token else None


def _written(header: str) -> list:
    """Units written as exact symbols in brackets ("Capacity (GB)") or after the last "_" ("volume_l")."""
    header = re.sub(r"\b[A-Za-z](?:\s*[x×*]\s*[A-Za-z])+\b", " ", header)   # "L x l x H" names axes, not litres
    inside = [t for part in re.findall(r"[(\[](.*?)[)\]]", header) for t in re.split(r"[\s,;]+", part) if t]
    suffix = header.rsplit("_", 1)[1] if "_" in header and " " not in header else None
    return [u for t in inside + ([suffix] if suffix else []) if (u := _lookup(t))]


def declared(header: str):
    """Fallback when GLiNER2 finds no span: one unit written in brackets or after the last "_" (see _written).
    Free words never count ("Price in store")."""
    found = _written(header)
    return found[0] if len({u["unit"] for u in found}) == 1 else None


# "per" in en/fr/de/es/it; "au" is French à + le ("Prix au kilo"). An optional quantity ("je 100 g"), then the unit.
PER = re.compile(r"(?:^|[\s_(\[])(?:per|par|pour|pro|je|por|al|au|a|à)[\s_]+(?:(\d+(?:[.,]\d+)?)[\s_]*)?([^\W\d_][^\s_()\[\]/]*)", re.I)
SLASH = re.compile(r"[^\s()\[\]]+/[^\s()\[\]]+")       # "EUR/kg", "€/100g"
# ponytail: the dictionary has no counting units; the piece is the only one "per" needs so far
PIECE = {"piece", "pieces", "pc", "pcs", "stuck", "stk", "pieza", "piezas", "pezzo", "pezzi"}


def _per(token: str):
    """The unit after the preposition: a piece, a unit name ("litre", "Meter"), or a symbol as written ("kg", "L")."""
    if plain(token).strip(".") in PIECE:
        return "piece"
    if u := unit_name(token):
        return u["unit"]
    u = _try(token, "physical_unit")
    return u["unit"] if u and u["unit"].casefold() == token.strip(".,;:").casefold() else None


def per_unit(header: str):
    """A price per unit the header writes out, read before GLiNER2 (which keeps only a part of it):
    - a currency/unit token: "Preis EUR/kg" → EUR/kg; one the dictionary can't read completely ("EUR/m2") gives
      unit None, never its currency alone;
    - "per X": "Precio por litro" → ?/l ("?": the currency is not written), "Price per kg (EUR)" → EUR/kg.
    None when the header writes neither, or when "per" belongs to another unit ("Weight per piece (g)")."""
    for token in SLASH.findall(header):
        if _try(token.split("/")[0].strip(".,;:"), "currency"):
            return _try(token) or {"kind": "compound_unit", "unit": None}
    for m in PER.finditer(header):
        if unit := _per(m[2]):
            break
    else:
        return None
    if any(u["kind"] == "physical_unit" and u["unit"] != unit for u in _written(header)):
        return None
    money = {u["unit"] for t in re.split(r"[\s()\[\]_/]+", header)
             if t and (t.isupper() or not t.isalpha()) and (u := _try(t, "currency"))}   # codes and symbols, not words
    quantity = (m[1] or "").replace(",", ".")
    return {"kind": "compound_unit", "unit": f"{money.pop() if len(money) == 1 else '?'}/{quantity}{unit}"}

