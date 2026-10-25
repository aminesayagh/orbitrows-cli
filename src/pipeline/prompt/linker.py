"""Value linker (step 3 candidates without GLiNER2, decided 2026-10-12 to benchmark: v2/bench/value_linking): the
request's own words and word sequences matched against the values the store holds, as text-to-SQL systems do
("value linking": BRIDGE's anchor text, CodeS, CHESS). It proposes (column, stored value) candidates with their span in
the prompt; Jev still decides each phrase's role and picks among candidates (values.py, call 1 and call 2).

    spans    every sequence of 1 to 3 words inside one request, and quoted text
    tiers    best first, per span and column: exact (ignoring case and accents) · typo (rapidfuzz, words of 4+
             letters) · contains (3+ letters, the span starts a word of a stored value: "Paper" → PaperHub) · meaning
             (multilingual embeddings, single words and pairs: "rouges" → red, "annulées" → cancelled)
    guards   contains and meaning only search columns of short values (names, statuses, colors: at most 4 words, not
             an e-mail): a word inside a description is not a value ("refunded" in a long text). Meaning is scored
             with CSLS (cross-lingual word matching): cosine corrected for hubness, since some short values ("Volt")
             sit close to every word; each value's hubness is measured on the store's own vocabulary (the words of
             its values and column names), never a hand-made word list
    keep     non-overlapping spans: word-level matches first, the longest stored value (maximal match), the best
             tier, the shortest span; then meanings, the closest first
    values   the profile's (pipeline/store/profile.py: LISTED at most per column), never the table's rows: a column of
             many values or of long texts is not searched
"""
import re
import unicodedata

import numpy as np
from rapidfuzz import fuzz

from orbitrows.pipeline.store.profile import listed
from .values import SEMANTIC_MODEL, TYPO, code, text_columns

MAX_WORDS = 3
CSLS_MIN = 0.45                    # calibrated on words outside the benchmark: translations ≥ 0.49, ordinary ≤ 0.33
CSLS_K = 10
RANK = {"exact": 0, "typo": 1, "contains": 2, "meaning": 3}
WORD = re.compile(r"[^\W_](?:[\w-]|['’](?=\w))*")         # an apostrophe only between letters: "don't", not "cotton'"

_CACHE = {}


def fold(t: str) -> str:
    return unicodedata.normalize("NFKD", t).encode("ascii", "ignore").decode().casefold().strip()


def _store(db, schema):
    """{column: [values]} (the profile's: a column of many values or long texts has none, never searched), the
    short-value columns, and the embeddings (values, hubness); the encoder and each text's vector once per process."""
    cols = {}
    for tid, tname, cid, cname in text_columns(schema):
        if vals := [str(v) for v in listed(db, tname, cname)]:
            cols[f"{tid}.{cid}"] = vals
    short = {k for k, vs in cols.items() if vs and all(len(v.split()) <= 4 and "@" not in v for v in vs)}
    if "enc" not in _CACHE:
        from sentence_transformers import SentenceTransformer
        _CACHE.update(enc=SentenceTransformer(SEMANTIC_MODEL), vectors={})
    pool = [(k, v) for k in sorted(short) for v in cols[k] if not code(v)]
    vocab = sorted({w for vs in cols.values() for v in vs for w in WORD.findall(v) if len(w) >= 3} |
                   {c["sql_name"] for t in schema for c in t["definition"]["columns"]} |
                   {t["definition"]["sql_name"] for t in schema})
    pv, vv = vectors([v for _, v in pool]), vectors(vocab)
    hub = np.sort(vv @ pv.T, axis=0)[-CSLS_K:].mean(axis=0) if pool else np.zeros(0)   # closeness to store words
    return {"values": cols, "short": short, "enc": _CACHE["enc"], "pool": pool, "pv": pv, "hub": hub}


def vectors(texts: list[str]):
    known = _CACHE["vectors"]
    if new := sorted(set(texts) - set(known)):
        known.update(zip(new, _CACHE["enc"].encode(new, normalize_embeddings=True)))
    return np.array([known[t] for t in texts]).reshape(len(texts), -1)


def spans(prompt: str, requests: list[dict]) -> list[tuple[int, int]]:
    out = []
    for r in requests:
        words = [m for m in WORD.finditer(prompt, r["start"], r["end"])]
        for i in range(len(words)):
            for n in range(1, MAX_WORDS + 1):
                if i + n <= len(words):
                    out.append((words[i].start(), words[i + n - 1].end()))
    return out


def typo(have: str, want: str) -> bool:
    """A spelling mistake, word by word when the word counts agree ("orbit hoodie's" ~ "orbit hoodie", not "orbit tee
    in" ~ "orbit tee kids": "in" is not a misspelled "kids"); the whole text when they differ ("tech source")."""
    if fuzz.ratio(have, want) < TYPO:
        return False
    hw, ww = have.split(), want.split()
    return len(hw) != len(ww) or all(fuzz.ratio(a, b) >= TYPO - 15 for a, b in zip(hw, ww))


def lexical(text: str, store) -> list[dict]:
    """Each column's best lexical tier for one span, best first: [{"column", "match", "stored"}]. Every column keeps
    its own match, so the sentence can still choose ("the Casa supplier": the brand Casa is exact, the supplier Casa
    Living partial; the word "supplier" decides)."""
    want, found = fold(text), {"exact": {}, "typo": {}, "contains": {}}
    letters = len(re.sub(r"[\W\d_]", "", want))
    for col, vals in store["values"].items():
        for v in vals:
            have = fold(v)
            if have == want:
                found["exact"].setdefault(col, []).append(v)
            elif letters >= 4 and typo(have, want):
                found["typo"].setdefault(col, []).append(v)
            elif letters >= 3 and col in store["short"] and re.search(rf"(?<![\w]){re.escape(want)}", have):
                found["contains"].setdefault(col, []).append(v)
    best = {}
    for kind in ("exact", "typo", "contains"):
        for c, vs in found[kind].items():
            best.setdefault(c, {"column": c, "match": kind, "stored": sorted(vs)})
    return sorted(best.values(), key=lambda f: RANK[f["match"]])


def meanings(texts: list[str], store) -> list[list[dict]]:
    """Per text, the values close in meaning (CSLS ≥ CSLS_MIN), best first, one candidate per value."""
    if not texts or not store["pool"]:
        return [[] for _ in texts]
    tv = store["enc"].encode(texts, normalize_embeddings=True)
    S = tv @ store["pv"].T
    rw = np.sort(S, axis=1)[:, -CSLS_K:].mean(axis=1)
    C = 2 * S - store["hub"][None, :] - rw[:, None]
    out = []
    for i in range(len(texts)):
        best = [j for j in np.argsort(-C[i])[:3] if C[i, j] >= CSLS_MIN]
        out.append([{"column": store["pool"][j][0], "match": "meaning", "stored": [store["pool"][j][1]],
                     "similarity": round(float(C[i, j]), 2)} for j in best])
    return out


def link(db, schema, prompt: str, requests: list[dict]) -> list[dict]:
    """Phrases with their candidates, as values.phrases() returns phrases: [{"id", "request", "text", "start",
    "end", "linked": [{"column", "match", "stored", "searched"}]}], ids l1, l2, …"""
    store = _store(db, schema)
    found = []
    sp = spans(prompt, requests)
    for a, b in sp:
        if c := lexical(prompt[a:b], store):
            found.append((a, b, c))
    sem = [(a, b) for a, b in sp if len(re.findall(WORD, prompt[a:b])) <= 2
           and len(re.sub(r"[\W\d_]", "", prompt[a:b])) >= 4]
    for (a, b), c in zip(sem, meanings([prompt[a:b] for a, b in sem], store)):
        if c:
            found.append((a, b, c))
    kept = []
    # whole-value matches (exact, typo) first: the longest STORED value ("Orbit Hoodie's" → Orbit Hoodie, not the
    # brand Orbit: BRIDGE's maximal match), then the best tier ("Desk Lamp by" typo loses to "Desk Lamp" exact: same
    # value), then the shortest span; partial matches next, the longest span; meanings last, the closest first
    def key(f):
        best = f[2][0]
        if best["match"] == "meaning":
            return (2, 0, RANK["meaning"], -best.get("similarity", 0), f[1] - f[0])
        if best["match"] == "contains":                       # a part of a value: after whole-value matches
            return (1, -(f[1] - f[0]), RANK["contains"], 0, 0)
        return (0, -max(len(v) for v in best["stored"]), RANK[best["match"]], 0, f[1] - f[0])
    for a, b, c in sorted(found, key=key):
        if all(b <= x or a >= y for x, y, _ in kept):
            kept.append((a, b, c))
    for q in re.finditer(r"'([^']+)'|\"([^\"]+)\"", prompt):           # a quoted literal: a value, maybe new
        a, b = q.span(1) if q.group(1) else q.span(2)
        if all(b <= x or a >= y for x, y, _ in kept):
            kept.append((a, b, []))
    out = []
    for i, (a, b, c) in enumerate(sorted(kept), 1):
        req = next((r["id"] for r in requests if r["start"] <= a and b <= r["end"]), None)
        out.append({"id": f"l{i}", "request": req, "text": prompt[a:b], "start": a, "end": b,
                    "linked": [f | {"searched": prompt[a:b]} for f in c]})
    return out
