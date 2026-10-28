"""Schema linking (notice #13): the columns a request names, so the steps that ask Jev about columns ask only about
those (step 5: which are shown; step 6: which choose the records) instead of every column of every table it reads.
Code only, no Jev: the columns are named by their sql names (no description is stored), split into words.

    find(text, schema, tables=None) → {"t_x.c_y": {"how": "word" | "meaning", "words": the request's words}}
        word     a word of the column's name, in its base form, written in the request ("the prices" → price_mad,
                 "heaviest"? no: "weight" must be written); a word naming a table is no column's ("variants" is not
                 variant_id), nor a word of two letters or less (a unit: the "g" of weight_g)
        meaning  a phrase of the request close in meaning to the column's name words (the meaning model the value
                 search loads, values.encoder): "how heavy" → weight_g, "stock" → quantity
"""
import re

from orbitrows.pipeline.expressions.words import nlp

MEANING_MIN = 0.6                                             # cosine of a phrase and a column's name words


def name_words(sql_name: str, table_words: set) -> list[str]:
    """A column's own words, in their base form: price_mad → [price, mad]; variant_id in variants → [] ("variant" is
    the table's, "id" too short)."""
    words = [w for w in re.split(r"[^a-z]+", sql_name.lower()) if len(w) > 2]
    return [w for w in (lemma(w) for w in words) if w not in table_words]


def lemma(word: str) -> str:
    return nlp("en")(word)[0].lemma_.lower()


def phrases(doc) -> list[str]:
    """The request's phrases that may name a column: its noun phrases (the whole and their head), its adjectives and
    adverbs ("heaviest", "how heavy")."""
    out = set()
    for chunk in doc.noun_chunks:
        out |= {chunk.text.lower(), chunk.root.lemma_.lower()}
    out |= {t.lemma_.lower() for t in doc if t.pos_ in ("ADJ", "ADV", "NOUN") and t.is_alpha and len(t) > 2}
    return sorted(out)


def find(text: str, schema, tables=None, meaning: bool = True) -> dict:
    doc = nlp("en")(text)
    said = {t.lemma_.lower() for t in doc if t.is_alpha}
    defs = [t["definition"] for t in schema if tables is None or t["definition"]["table_id"] in tables]
    table_words = {lemma(w) for t in schema for w in re.split(r"[^a-z]+", t["definition"]["sql_name"].lower()) if w}
    cols = {f"{d['table_id']}.{c['column_id']}": name_words(c["sql_name"], table_words) for d in defs
            for c in d["columns"]}
    out = {k: {"how": "word", "words": [w for w in ws if w in said]} for k, ws in cols.items()
           if any(w in said for w in ws)}
    if meaning:
        from .values import encoder
        ps = phrases(doc)
        named = {k: " ".join(ws) for k, ws in cols.items() if ws and k not in out}
        if ps and named:
            enc = encoder()
            pv = enc.encode(ps, normalize_embeddings=True)
            cv = enc.encode(list(named.values()), normalize_embeddings=True)
            sims = pv @ cv.T
            for j, k in enumerate(named):
                i = int(sims[:, j].argmax())
                if sims[i, j] >= MEANING_MIN:
                    out[k] = {"how": "meaning", "words": [ps[i]], "score": round(float(sims[i, j]), 2)}
    return out
