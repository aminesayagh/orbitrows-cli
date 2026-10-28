"""Schema linking (notice #13): the columns a request names, so the steps that ask Jev about columns ask only about
those (step 5: which are shown; step 6: which choose the records) instead of every column of every table it reads.
Code only, no Jev: the columns are named by their sql names (no description is stored), split into words.

    find(text, schema, tables=None, db=None) → {"t_x.c_y": {"how": "word" | "meaning" | "kind", "words": …}}
        word     a word of the column's name, in its base form, written in the request ("the prices" → price_mad,
                 "heaviest"? no: "weight" must be written); a word naming a table is no column's ("variants" is not
                 variant_id), nor a word of two letters or less (a unit: the "g" of weight_g)
        meaning  a phrase of the request close in meaning to the column's name words (the meaning model the value
                 search loads, values.encoder): "how heavy" → weight_g, "stock" → quantity
        kind     a phrase naming columns by their kind ("all the number columns", "the dates", "the ids"): close in
                 meaning to a kind's name (KINDS) and said as a group, plural or with a word of totality (spaCy's
                 Number=Plur, PronType=Tot: "the dates", never "the sale date"); the kind's columns of the first of
                 `tables` (the request's main table) come from what is stored, no row read: numbers (INTEGER, REAL)
                 and texts (TEXT, not dates) but keys, dates (the import's profile), ids (its primary key)
    run(requests, schema, sources, db)   the step: each request's columns, for steps 5 and 6 (section "columns")
Measured (.bench/columns_recall.py, groups_proto.py): every non-key column the expected SQL uses found or reached
through a written value; group phrases 0.89–0.98 to their kind, other phrases at most 0.68.
"""
import re

from orbitrows.pipeline.expressions.words import nlp

MEANING_MIN = 0.6                                             # cosine of a phrase and a column's name words
KINDS = {"numbers": "number columns", "texts": "text columns", "dates": "dates", "ids": "ids"}
KIND_MIN = 0.8                                                # cosine of a group phrase and a kind's name


def name_words(sql_name: str, table_words: set) -> list[str]:
    """A column's own words, in their base form: price_mad → [price, mad]; variant_id in variants → [] ("variant" is
    the table's, "id" too short)."""
    words = [w for w in re.split(r"[^a-z]+", sql_name.lower()) if len(w) > 2]
    return [w for w in (lemma(w) for w in words) if w not in table_words]


def unit_word(sql_name: str) -> str | None:
    """The unit written after the column name's last "_" (the unit dictionary's reading, a code in any case: price_mad
    → mad), else None. A written unit names the column ("under 150 MAD"); its meaning is no name's: "cheap" is 0.71 to
    price, 0.55 to "price mad" (2026-10-28, step 2.2)."""
    from orbitrows.units.dictionary import declared
    if "_" not in sql_name:
        return None
    head, last = sql_name.rsplit("_", 1)
    return last.lower() if declared(sql_name) or declared(f"{head}_{last.upper()}") else None


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


def group_phrases(doc) -> list[str]:
    """Noun phrases said as a group: a plural head, or a word of totality (all, every, each) in or just before it."""
    out = []
    for c in doc.noun_chunks:
        whole = any("Tot" in t.morph.get("PronType") for t in doc[max(c.start - 2, 0):c.end])
        if "Plur" in c.root.morph.get("Number") or whole:
            out += [c.text.lower(), c.root.lemma_.lower()]
    return out


def kind_columns(db, schema, table, kind) -> list[str]:
    """A kind's columns of `table`, from what is stored about them (types, keys, the import's profile)."""
    from orbitrows.pipeline.store.profile import facts
    d = next(t["definition"] for t in schema if t["definition"]["table_id"] == table)
    keys = set(d.get("primary_key", [])) | {c for fk in d.get("foreign_keys", []) for c in fk["columns"]}
    out = []
    for c in d["columns"]:
        date = bool(facts(db, d["sql_name"], c["sql_name"]).get("dates")) if db is not None else False
        if {"numbers": c["sql_type"] in ("INTEGER", "REAL") and c["column_id"] not in keys,
                "texts": c["sql_type"] == "TEXT" and not date and c["column_id"] not in keys,
                "dates": date, "ids": c["column_id"] in d.get("primary_key", [])}[kind]:
            out.append(f"{table}.{c['column_id']}")
    return out


def find(text: str, schema, tables=None, meaning: bool = True, db=None) -> dict:
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
        units = {f"{d['table_id']}.{c['column_id']}": unit_word(c["sql_name"]) for d in defs for c in d["columns"]}
        named = {k: " ".join(w for w in ws if w != units[k]) for k, ws in cols.items()   # by meaning: the name
                 if [w for w in ws if w != units[k]] and k not in out}                    # without its unit
        if ps and named:
            enc = encoder()
            pv = enc.encode(ps, normalize_embeddings=True)
            cv = enc.encode(list(named.values()), normalize_embeddings=True)
            sims = pv @ cv.T
            for j, k in enumerate(named):
                i = int(sims[:, j].argmax())
                if sims[i, j] >= MEANING_MIN:
                    out[k] = {"how": "meaning", "words": [ps[i]], "score": round(float(sims[i, j]), 2)}
        if tables and (gs := group_phrases(doc)):             # a kind of columns, of the main table
            enc = encoder()
            sims = enc.encode(gs, normalize_embeddings=True) @ enc.encode(list(KINDS.values()),
                                                                          normalize_embeddings=True).T
            for j, kind in enumerate(KINDS):
                i = int(sims[:, j].argmax())
                if sims[i, j] >= KIND_MIN:
                    for k in kind_columns(db, schema, tables[0], kind):
                        out.setdefault(k, {"how": "kind", "words": [gs[i]], "kind": kind,
                                           "score": round(float(sims[i, j]), 2)})
    return out


def run(requests, schema, sources: dict, db) -> dict:
    """The step: each request's columns, its main table's first (step 4's target or source, then what it reads)."""
    from .parts import main_table, read_tables              # step 5's tables: the main one, what it reads, parents
    out = {}
    for r in requests:
        main = main_table(requests, sources, r["id"])
        out[r["id"]] = find(r["text"], schema, read_tables(schema, sources, r["id"], main) if main else None, db=db)
    return {"requests": out}
