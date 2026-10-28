"""The column finder by words (no model loaded): a column's name words in their base form; a table's word or a short
unit is no column's."""
from fixtures.orbitrow.load import baseline
from orbitrows.pipeline.prompt import columns
from orbitrows.pipeline.prompt.pipeline import store_of


def test_columns_named_by_their_words():
    schema = store_of(baseline())
    found = lambda text: {k.split(".c_")[1] for k in columns.find(text, schema, meaning=False)}
    assert {"price_mad", "unit_price_mad", "weight_g"} <= found("Lower the prices of the variants heavier than 60 g "
                                                                "and show their weight")
    assert "variant_id" not in found("Show the variants")                 # "variant" is the table's word
    assert found("Archive the Casa Living products") == set()             # a value names no column (step 3's)
