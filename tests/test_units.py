import os

import pytest

from orbitrows.units.header import resolve_header

pytestmark = pytest.mark.usefixtures("no_gliner")
KNOWN = pytest.mark.xfail(strict=True, reason="known miss (see doc/to-review.md)")


@pytest.mark.parametrize("header, category, expected", [
    ("Weight unit (kg)", "currency", None),  # a "both" column only accepts its own category
    ("Price", None, None),
    pytest.param("Prix (DH)", None, ("currency", "MAD"), marks=KNOWN),  # "DH" isn't a Babel symbol
])
def test_resolve_header(header, category, expected):
    found = resolve_header(header, category)
    assert (found and (found["kind"], found["unit"])) == expected


@pytest.mark.skipif(not os.environ.get("GLINER"), reason="loads GLiNER2 (about 9 s); run with GLINER=1")
@pytest.mark.parametrize("header, expected", [
    ("Grundpreis (€/kg)", ("compound_unit", "EUR/kg")),
    ("Prix au kilo (€/kg) en EUR", ("compound_unit", "EUR/kg")),  # EUR agrees with €/kg
])
def test_resolve_header_with_the_real_extractor(monkeypatch, header, expected):
    monkeypatch.undo()   # the module's stub extractor off: the real GLiNER2
    from orbitrows.services.extractor import client
    client._loaded.pop("gliner2", None)
    found = resolve_header(header)
    assert (found and (found["kind"], found["unit"])) == expected


# Review of the resolver: dimension checks, label-aware lookup, no guessed ambiguity, complete compounds.
from orbitrows.units.header import lookup, skipped_units  # noqa: E402


def test_cldr_units_keep_their_dimension():
    skipped = dict(skipped_units())
    assert "volume-dram" in skipped and "volume-pinch" in skipped  # Pint reads a mass and a length


@pytest.mark.parametrize("token, kind, expected", [
    ("pt", "language", ("language", "pt")),  # Portuguese, not a pint
    ("€", None, ("currency", "EUR")),  # one currency only
    ("fr/kg", None, None),  # a language is not a price numerator
])
def test_lookup(token, kind, expected):
    found = lookup(token, kind)
    assert (found and (found["kind"], found["unit"])) == expected


@pytest.mark.parametrize("header, expected", [
    ("Prix (EUR) (USD)", None),  # independent declarations that disagree
])
def test_resolve_header_spans(header, expected):
    found = resolve_header(header)
    assert (found and (found["kind"], found["unit"])) == expected


@pytest.mark.parametrize("parts, how, expected", [
    ([("currency", "EUR"), ("qty", "100"), ("physical_unit", "g")], "rate", ("compound_unit", "EUR/100g")),
    ([("currency", "EUR"), ("currency", "USD")], "rate", None),  # not a rate's shape
    ([("physical_unit", "m"), ("physical_unit", "m")], "multiply", ("physical_unit", "m ** 2")),
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
    ("Cost USD/l", "USD/l"), ("Price per litre", "?/l"), ("Price per kg (EUR)", "EUR/kg"),
])
def test_per_unit(header, expected):
    got = per_unit(header)
    assert got and got["kind"] == "compound_unit" and got["unit"] == expected


@pytest.mark.parametrize("header", [
    "Weight per piece (g)", "Taille (S/M/L)", "Volume (L)",
])
def test_per_unit_none(header):
    assert per_unit(header) is None


def test_declared():
    assert declared("Capacity (GB)") == {"kind": "physical_unit", "unit": "GB"} and declared("Price per kg") is None
