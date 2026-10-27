"""coref's mentions by spaCy's grammar: what Jev is offered to decide (whole phrases, pronouns, "that one"), and
what it never is (a phrase's bare head, a relative pronoun)."""
from orbitrows.pipeline.prompt import coref


def offered(prompt):
    return {m["text"] for m in coref.mentions([prompt])[0]}


def test_mentions_are_whole_phrases_pronouns_and_arguments():
    m = offered("raise the price of the Bose speakers, then delete them")
    assert {"the price of the Bose speakers", "the Bose speakers", "them"} <= m and "the price" not in m
    assert "the ones" in offered("delete the Logitech products, then hide the ones with no stock")   # as parsed
    assert "that one" in offered("SKU A-1001 has a typo. Fix that one.")
    assert "that" not in offered("delete every product that has no ean")
