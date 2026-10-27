import pytest

from orbitrows.pipeline.expressions.steps import iso
from test_units_dates import NEEDS_DUCKLING

pytestmark = NEEDS_DUCKLING                  # datephrase reads with the Duckling service


@pytest.mark.parametrize("text, expected", [
    ("2026-10-26", "2026-10-26"),            # the three formats DATE finds
    ("26 October 2026", "2026-10-26"),
    ("October 26, 2026", "2026-10-26"),
    ("01/02/2026", None),                    # January 2 or February 1: the reader asks, so no date here
    ("26/10/2026", "2026-10-26"),            # no month 26: read day first
    ("2026-10-26 14:30", "2026-10-26"),      # the time of day is dropped
    ("2026-02-29", None),                    # no such day
    ("cotton", None),
])
def test_iso(text, expected):
    assert iso(text) == expected


@pytest.mark.parametrize("text", ["tomorrow", "October 26"])
def test_iso_fills_from_the_current_date(text):
    assert iso(text) is not None             # the value depends on today: only its presence is stable
