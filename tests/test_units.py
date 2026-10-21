import pytest

from orbitrows.units.header import resolve_header

KNOWN = pytest.mark.xfail(strict=True, reason="known miss (see doc/to-review.md)")


@pytest.mark.parametrize("header, category, expected", [
    ("Poids (kg)", None, ("physical_unit", "kg")),
    ("Volume (litres)", None, ("physical_unit", "l")),
    ("Poids (kilos)", None, ("physical_unit", "kg")),  # CLDR name, not Pint's kiloseconds
    ("Grundpreis (€/kg)", None, ("compound_unit", "EUR/kg")),
    ("Price EUR/kg", None, ("compound_unit", "EUR/kg")),
    ("Prix (€)", None, ("currency", "EUR")),
    ("Price in EUR", None, ("currency", "EUR")),  # "in" is not read as inch
    ("Langue (fr)", None, ("language", "fr")),
    ("Taux TVA (%)", None, ("percentage", "%")),
    ("Weight unit (kg)", "physical_unit", ("physical_unit", "kg")),
    ("Weight unit (kg)", "currency", None),  # a "both" column only accepts its own category
    ("Dimensions (L x l x H)", None, None),  # axis labels are not units
    # Benchmark bugs (poc/docs/header-context-benchmark.md)
    ("Poids (g)", None, ("physical_unit", "g")),  # not µg
    ("Hauteur (m)", None, ("physical_unit", "m")),  # not µm
    ("Capacité (ml)", None, ("physical_unit", "ml")),  # not Ml (megalitre)
    ("Prix €", None, ("currency", "EUR")),  # symbols outside brackets
    ("Remise %", None, ("percentage", "%")),
    ("Langue (ar)", None, ("language", "ar")),  # not "are"/au
    pytest.param("Prix (DH)", None, ("currency", "MAD"), marks=KNOWN),  # "DH" isn't a Babel symbol
    ("price_eur", None, ("currency", "EUR")),  # lowercase code as a snake_case suffix
    ("POIDS (KG)", None, ("physical_unit", "kg")),  # not the language Kongo
    ("Poids (kgs)", None, ("physical_unit", "kg")),  # not the Kyrgyz som
    ("Note (sur 5)", None, None),  # "sur" is not the Soviet rouble
    ("VAT rate", None, None),  # "vat" is a barrel in Dutch
    ("Stock (en magasin)", None, None),  # "en" is a word here, not a language code
    ("Price (Swiss francs)", None, ("currency", "CHF")),  # not the language fr
    ("Price", None, None),
    ("Currency", None, None),
    ("Description language", None, None),
])
def test_resolve_header(header, category, expected):
    found = resolve_header(header, category)
    assert (found and (found["kind"], found["unit"])) == expected


# Review of the resolver: dimension checks, label-aware lookup, no guessed ambiguity, complete compounds.
from orbitrows.units.header import lookup, skipped_units  # noqa: E402


def test_cldr_units_keep_their_dimension():
    skipped = dict(skipped_units())
    assert "volume-dram" in skipped and "volume-pinch" in skipped  # Pint reads a mass and a length


@pytest.mark.parametrize("token, kind, expected", [
    ("pt", "language", ("language", "pt")),  # Portuguese, not a pint
    ("lb", "language", ("language", "lb")),  # Luxembourgish, not a pound
    ("pt", "physical_unit", ("physical_unit", "pt")),
    ("$", None, ("currency", "$")),  # USD, CAD, AUD…: kept as written
    ("¥", None, ("currency", "¥")),  # JPY or CNY
    ("€", None, ("currency", "EUR")),  # one currency only
    ("fr/kg", None, None),  # a language is not a price numerator
    ("EUR/100g", None, ("compound_unit", "EUR/100g")),  # the quantity is kept
    ("EUR/l", "currency", ("compound_unit", "EUR/l")),  # labelled currency, still a price per unit
])
def test_lookup(token, kind, expected):
    found = lookup(token, kind)
    assert (found and (found["kind"], found["unit"])) == expected


@pytest.mark.parametrize("header, expected", [
    ("Grundpreis (€/100 g)", ("compound_unit", "EUR/100g")),
    ("Prix au kilo (€/kg) en EUR", ("compound_unit", "EUR/kg")),  # EUR agrees with €/kg
    ("Prix (EUR) (USD)", None),  # independent declarations that disagree
])
def test_resolve_header_spans(header, expected):
    found = resolve_header(header)
    assert (found and (found["kind"], found["unit"])) == expected


@pytest.mark.parametrize("parts, how, expected", [
    ([("currency", "EUR"), ("qty", "100"), ("physical_unit", "g")], "rate", ("compound_unit", "EUR/100g")),
    ([("currency", "EUR"), ("physical_unit", "kg")], "rate", ("compound_unit", "EUR/kg")),
    ([("physical_unit", "kg"), ("currency", "EUR")], "rate", ("compound_unit", "EUR/kg")),  # a price is per unit
    ([("currency", "EUR"), ("currency", "USD")], "rate", None),  # not a rate's shape
    ([("physical_unit", "m"), ("physical_unit", "m")], "multiply", ("physical_unit", "m ** 2")),
    ([("currency", "EUR"), ("physical_unit", "kg")], "multiply", None),  # only physical units multiply
])
def test_combine(parts, how, expected):
    from orbitrows.units.header import _combine
    assert _combine(parts, how) == expected


# Cell units and per-unit headers (units/dictionary.py)
from orbitrows.units.dictionary import cell_unit, declared, per_unit  # noqa: E402


def test_cell_unit():
    assert cell_unit("16,50 €") == (16.5, {"kind": "currency", "unit": "EUR"})
    assert cell_unit("extra 1 L") is None


@pytest.mark.parametrize("header, expected", [
    ("Cost USD/l", "USD/l"), ("Kosten EUR/m", "EUR/m"), ("Preis EUR/kg", "EUR/kg"), ("Prix au litre (€/L)", "EUR/l"),
    ("EUR/m2", None), ("Price per litre", "?/l"), ("Precio por litro", "?/l"), ("Prix par mètre", "?/m"),
    ("Preis pro Stück", "?/piece"), ("Price per kg", "?/kg"), ("Price per kg (EUR)", "EUR/kg"), ("price_per_kg", "?/kg"),
    ("Preis je 100 g", "?/100g"), ("Prezzo al kg (€)", "EUR/kg"), ("Prix au kilo", "?/kg"), ("Price (per L)", "?/l"),
])
def test_per_unit(header, expected):
    got = per_unit(header)
    assert got and got["kind"] == "compound_unit" and got["unit"] == expected


@pytest.mark.parametrize("header", [
    "Weight per piece (g)", "Price in store", "Stock per store", "Taille (S/M/L)", "Poids/kg", "Type (in/out)",
    "Weight unit (kg)", "Currency (EUR)", "Volume (L)", "length_cm", "Capacity (GB)", "Price (a)",
])
def test_per_unit_none(header):
    assert per_unit(header) is None


def test_declared():
    assert declared("Capacity (GB)") == {"kind": "physical_unit", "unit": "GB"} and declared("Price per kg") is None
