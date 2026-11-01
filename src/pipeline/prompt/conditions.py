"""Step 6 of prompt → AST: which columns choose each request's rows and how (plan S5), and whether a read is sorted,
limited or grouped (S7). Stage 1: the columns. Stage 2: one condition per column. The all / any tree and the sort,
limit and group columns are the next stage.

    Jev   one call per prompt, per request that reads or changes rows (select, update, delete):
            does it act on every record (no condition)?
            per column of its tables (step 4: main table and reads): is it used to choose the records?
            per table linked to its main table by a key: does it keep the records with, or without, a matching
            record there ("supplier SKUs not in the store yet", "products without variants")?
            select only: are the records sorted? limited to a number? grouped (per …)?
    code  a lookup value of step 3 tied to a column chooses rows by definition: that column is kept even when Jev
          says no (SQLite found the value there); a column the request changes (step 5) is shown to Jev, since it can
          also choose ("raise the price of the variants priced under 100"); below 0.8 an answer is kept and flagged

Stage 2, per condition column:
    code  its operand: step 3's values found in this column are SQL's answer, used as they are (one value's stored
          spellings: "volt" → Volt, volt; several values: IN), and Jev is not asked for the value; otherwise
          candidates, never guessed: the request's numbers (parameters algorithm, v2/units) whose unit fits the column's
          (the import's header unit: "1 kg" → 1000 for weight_g; a unit of another kind is never offered; a number
          step 5 writes as a new value is not a condition's), ISO dates, its table's other columns of the same type,
          and those of the tables a record of it has one record of (linked(): "below the supplier's recommended
          price"), the request's texts (patterns: "starts with Mug")
    Jev   the operator (among those its type allows), the operand, the upper bound of a range, negated or not
    code  the AST node (comparison · membership · between · pattern with % and _ escaped · empty_check ·
          null_check), `not` around a negated one; negated on a column that can be NULL keeps the NULL rows
          ("except the Orbit ones" keeps the product without a brand: SQL's NOT (NULL = 'Orbit') would drop it);
          a comparison with a linked table's column is correlated through the keys (through(): price >
          recommended → sku IN (SELECT o.sku FROM offer o WHERE price > o.recommended)), so it stays a condition on
          its own table; a matching record (stage 1) is key [NOT] IN (SELECT its key FROM the other table WHERE it
          IS NOT NULL: a NULL key there never defeats NOT IN);
          then each condition is prepared on its table (sql.check, no row read): an error is flagged
    ponytail: relative dates ("last month", "in October") are not read: flagged; add with a session date

Section "conditions" of cases/<id>.json: {"requests": {"r1": {"all_rows": bool, "columns": ["t_x.c_y"], "sorted"?,
"limited"?, "grouped"?, "linked"?: [{"table", "has", "of"}], "predicates": [{"column", "operator", "negated", "condition": AST,
"rows", "linked"?}], "flagged"?}},
"warnings", "jev": {"stage_1", "stage_2"}}.
"""
import functools
import json
import re

from orbitrows.sql.sql import check, program
from orbitrows.pipeline.store.context import declaration
from orbitrows.pipeline.store.profile import facts, listed
from orbitrows.units.header import _tables
from orbitrows.units.parameters import parameters
from .values import search, within


def meant(db, schema, text: str) -> list[dict]:
    """The value linker's meaning matches for a text's words and pairs (CSLS, linker.py), best first."""
    from .linker import WORD, _store, meanings
    store = _store(db, schema)
    words = WORD.findall(text)
    spans = words + [f"{a} {b}" for a, b in zip(words, words[1:])]
    found = [f for fs in meanings(spans, store) for f in fs]
    return sorted(found, key=lambda f: -f.get("similarity", 0))
from . import SURE
from .asking import question
from orbitrows.services.model.client import ask, choice, yes_no
from .parts import columns_of, main_table
from .relations import chained, linked, paths, relationships
from orbitrows.units import dates
from orbitrows.units.quantity import find, plain

CONTEXT = ("A user wrote a prompt to edit or read their store database (tables and columns in `tables`). It was "
           "split into the requests in `requests`, each with its statement, its table, the values it names and where "
           "the store holds them, and the columns it changes. Each request now needs which records it acts on.")
ALL_RULES = [
    "Answer yes when the request acts on every record of its table: 'increase every price by 2%', 'show all "
    "suppliers', 'the five cheapest variants' (sorted, but no record is excluded before sorting).",
    "Answer no when any word restricts the records: a value ('the black ones'), a comparison ('under 100'), a missing "
    "value ('without a description'), a link to other records ('the products of Kora'), an exception ('except …').",
]
CHOOSE_RULES = [
    "`starts_from`: the request starts from the records another request chose ('those', 'them'); it chooses no "
    "records again: answer yes only for a condition written in its own words ('of those, only the red ones').",
    "Answer yes when the request keeps or excludes records by this column: 'the black variants' (color), 'priced at "
    "250' (price), 'without a barcode' (barcode), 'except the Kora ones' (the column holding Kora), 'newer than "
    "June' (the date).",
    "Answer no for a column the request only changes ('set the price to 199': price is not a condition, unless "
    "another words choose by it), only shows, only sorts by ('the cheapest'), or only groups by ('per brand').",
    "A column of another table counts when the condition is on it: 'the variants of the Kora products' (the brand "
    "or name holding Kora).",
    "Answer no when the request counts, totals or checks the existence of records in another table rather than "
    "reading this column: 'never sold', 'without any order', 'more than 3 variants', 'whose total stock is under 20' "
    "are about having, or how many or how much of, the linked records, not about a date or a quantity column being "
    "empty or compared on each record.",
]
MODIFIERS = {
    "sorted": "Are the records put in an order ('the cheapest', 'newest first', 'by price', 'the top 3')?",
    "limited": "Is the answer limited to a number of records ('the five cheapest', 'the top 3', 'only one')?",
    "grouped": "Are the records grouped to give one answer per group ('per brand', 'for each warehouse', 'by "
               "status')?",
}
ROWS = ("select", "update", "delete")
OPERATORS = {
    "=": "Equal to a value ('the black ones', 'in Rabat', 'priced at 250', also 'not black': the negation is asked "
         "apart), or inside a period ('in August', 'last month').",
    "<": "Lower than ('under 300', 'cheaper than', 'before').", "<=": "At most ('300 or less', 'up to').",
    ">": "Greater than ('over 300', 'heavier than', 'after').", ">=": "At least ('300 or more', 'from').",
    "between": "Between two bounds, both included ('between 200 and 500').",
    "in": "One of several values ('black or navy', 'red, blue or white').",
    "contains": "Contains a text.", "starts_with": "Starts with a text.", "ends_with": "Ends with a text.",
    "is_empty": "Has no value ('without a description', 'no barcode').",
    "is_not_empty": "Has a value ('with a barcode').",
}
TEXT_ONLY = {"contains", "starts_with", "ends_with"}
ONE = ("between", "in", "is_empty", "is_not_empty")          # a mention is one value: never these
MENTION_RULES = [
    "Each value is its own condition; how the conditions combine (AND, OR) is asked later.",
    "A bound of a range is its own condition: 'between 100 and 150' is at least 100 and at most 150.",
    "Answer none when this value does not choose records by this column (it belongs to another column, or it "
    "is a new value, a limit or a quantity, or the amount of a change: 'lower the price by 10%').",
    "A negation belongs to the condition whose own words negate it ('but not 120' negates only 120; 'except the "
    "Kora ones' negates only Kora), never to the other values of the column.",
]


def around(r, key, values, params, m) -> str:
    """The request's words around mention m of `key` (operands()' numbering), for Jev: which words go with it."""
    kind, i, t, at = m[0], int(m[1:]), r["text"], r.get("start", 0)
    if kind == "s":
        v = [v for v in values if v["request"] == r["id"] and v.get("column") == key][i]
        a, b = v["start"] - at, v["end"] - at
    elif kind == "n":
        a, b = params[i]["start"], params[i]["end"]
    else:
        a, b = r["dates"][i]["start"], r["dates"][i]["end"]
    return f"'…{t[max(0, a - 30):b].strip()}'"                # its words come before it ('over 100', 'heavier than 1 kg')


def computed() -> str:
    """The "computed" option: a value computed rather than the column's own, described by the catalog's own conditions
    (a condition enabled there is offered here at once; 2026-10-28: a hand-written list hid the newer ones)."""
    from orbitrows.pipeline.operations import use
    if "computed" not in _CATALOG:
        names = [e["name"].lower() for e in use.catalog()["entries"].values() if e["role"] == "condition"
                 and e["builder_key"] != "linked_aggregate"]          # the linked records' comparison: stage 2c's
        _CATALOG["computed"] = ("A value computed from the column, or from it and another column of the record, rather "
                                "than the column's own value: " + "; ".join(names) + ".")
    return _CATALOG["computed"]
COMPUTED_RULES = [
    "The operation is the computation the condition compares with; which records, and showing, deleting or "
    "updating them, are not part of it.",
    "Choose none when no candidate computes what the request compares with (a median, a percentile, a rank).",
]


def mentions(cands: dict) -> list[str]:
    """The operands the request writes for a column: step 3's values found in it, its numbers, its dates. Two or
    more are as many conditions ("under 100 or over 300", "the black ones … or the white ones …"), each asked
    apart; grouping joins them. One or none: the column's one condition, as before."""
    return [k for k in cands if k[0] in "snd"]
NEGATE_RULES = [
    "Answer yes only when words negate the condition: 'except the Kora ones', 'not black', 'other than', "
    "'excluding', 'all but'.",
    "Answer no when no word negates it, whatever the request then does with the records: 'delete the refunded "
    "sales' (refunded, not negated).",
    "Answer no for 'without a description' or 'no barcode': that is the condition 'is empty', not a negation.",
]


EXCLUDE_RULES = [
    "Answer yes when the request's words leave these records out: 'except the refunded ones', 'not black', "
    "'other than', 'excluding'.",
    "Answer no when the request keeps these records: a negation written for another condition does not leave them "
    "out, and words the condition already expresses do not either ('not under 100' written as price >= 100 keeps "
    "the records whose price >= 100).",
]


def plain_of(cond: dict) -> dict:
    """A negated condition without its negation (and the NULL rows a negation keeps on a nullable column)."""
    if cond.get("type") == "any":
        cond = next(c for c in cond["children"] if c.get("type") == "not")
    return cond["operand"]


def questions_2n(requests, schema, conds) -> tuple[dict, list]:
    """Each negated condition, shown as built, asked again: does the request leave these records out (2026-10-29:
    a bare yes/no per column negated "October" in "the October sales except the refunded ones", and negated "not
    below" a second time once it was written >=)? Asked in stage 2b's call."""
    from .grouping import readable
    q, asked = {}, []
    for r in requests:
        for i, p in enumerate((conds.get(r["id"]) or {}).get("predicates", [])):
            if p.get("negated") and "linked" not in p:
                key = f"{r['id']}|kept{i}"
                q[key] = yes_no(CONTEXT, f"In the request '{r['text']}', are the records where "
                                         f"{readable(schema, p['column'].split('.')[0], plain_of(p['condition']))} "
                                         f"left out?", EXCLUDE_RULES)
                asked.append((key, r["id"], p))
    return q, asked


def build_2n(section, asked, answers):
    """Not left out: the condition without its negation; unsure: the user is asked."""
    for key, rid, p in asked:
        y = answers.get(key, 1.0)
        if y < 1 - SURE:
            p["condition"], p["negated"] = plain_of(p["condition"]), False
        elif y < SURE:
            section["requests"][rid]["flagged"] = True
            section.setdefault("warnings", []).append({"request": rid, "problem": f"negation of {p['column']} "
                                                       "uncertain", "probability": round(y, 2)} | question(answers, key))


OWNER_RULES = [
    "Written words belong to one condition: the one the words around them describe ('created in May': the "
    "creation date; 'costing 40': the cost).",
]


def questions_2x(requests, schema, conds) -> tuple[dict, list]:
    """Words two columns' conditions both took (2026-10-29: "created in September … sold in September" put both
    Septembers on both dates, "heavier than 250 grams or priced at 250" the price's 250 on the weight too): which
    condition they belong to, asked once with both shown. Asked in stage 2b's call."""
    from .grouping import readable
    q, asked = {}, []
    for r in requests:
        preds, spans = (conds.get(r["id"]) or {}).get("predicates", []), {}
        for i, p in enumerate(preds):
            for w in {tuple(w) for w in p.get("words", [])}:
                spans.setdefault(w, []).append(i)
        for (a, b), idx in spans.items():
            if len({preds[i]["column"] for i in idx}) < 2:
                continue
            key, t = f"{r['id']}|owner{a}", r["text"]
            q[key] = choice(CONTEXT, f"In the request '{t}', which condition do the words '{t[a:b]}' (in "
                                     f"'…{t[max(0, a - 30):b]}') belong to?",
                            {str(i): readable(schema, preds[i]["column"].split(".")[0], preds[i]["condition"])
                             for i in idx}, OWNER_RULES)
            asked.append((key, r["id"], {str(i): preds[i] for i in idx}))
    return q, asked


def build_2x(section, asked, answers):
    """Sure: the other one-occurrence conditions on those words are dropped; unsure: kept, the ledger refuses."""
    for key, rid, preds in asked:
        pick, probs = answers.get(key, (None, {}))
        if probs.get(pick, 0) < SURE:
            continue
        own = section["requests"][rid]["predicates"]
        lost = [p for i, p in preds.items() if i != pick and p.get("mention")]
        section["requests"][rid]["predicates"] = [p for p in own if not any(p is x for x in lost)]


COMPARES = ("Only those whose records of {0} compare with a number: how many there are, or a total, an average, "
            "a lowest or a highest value of them ('more than 3 {0}', 'whose {0} total over 1000', 'whose cheapest "
            "of the {0} is under 100'), or whether all of them meet a condition ('whose {0} are all black').")


def has_options(other: str) -> dict:
    return {"has": f"Only those with a matching record in {other} ('already in {other}', 'that exist in {other}').",
            "has_not": f"Only those with no matching record in {other} ('not in {other} yet', 'missing from "
                       f"{other}', 'without any {other}').",
            "neither": "Neither: it does not choose by that (a value of that table, 'the Kora ones', is a condition "
                       "on its column)."}


def keyed_to(schema, table) -> dict:
    """{other table: (this table's column, its column)}: the first link to each table (relationships())."""
    out = {}
    for r in relationships(schema, table):
        out.setdefault(r["other"], (r["mine"], r["theirs"]))
    return out


def _sub(table, alias, column, where) -> dict:
    return {"id": alias, "type": "select", "from": {"type": "database_table", "reference": table, "alias": alias},
            "columns": [{"expression": {"type": "column", "source": alias, "reference": column}, "alias": "k"}],
            "scope": {"type": "filtered", "where": where}}


def through(schema, cond, table) -> dict:
    """A condition on `table` naming a linked table's column → on `table` alone, correlated through the keys: price >
    recommended → sku IN (SELECT o.sku FROM offer o WHERE price > o.recommended); a parent's: product_id IN (SELECT
    p.id FROM products p WHERE price < p.base_price). The subqueries' own aliases are not tables: assembly lifts the
    condition as one on `table`."""
    chains = linked(schema, table)
    for other in sorted({s for s in re.findall(r'"source": "([^"]+)"', json.dumps(cond)) if s in chains}):
        chain, n = chains[other], json.dumps(cond).count('"type": "select"')
        cond = chained(chain, lambda a, c=cond, o=other: json.loads(json.dumps(c).replace(f'"source": "{o}"',
                                                                                         f'"source": "{a}"')),
                       table, lambda i, t, n=n: f"{t}_l{n}{i}", _sub)
    return cond


def existence_via(table, mine, via, other, theirs, has: bool) -> tuple[str, dict]:
    """Two links away ("products never sold": products ← variants ← sales): mine [NOT] IN (SELECT the middle's key to
    this table FROM middle WHERE its id IN (SELECT the final table's key WHERE it IS NOT NULL))."""
    chain = [(table, mine, via["table"], via["key"]), (via["table"], via["id"], other, theirs)]
    return f"{table}.{mine}", known_in(chain, has)


def existence(schema, table, other, has: bool, link=None) -> tuple[str, dict]:
    """(the key column, its condition): key [NOT] IN (SELECT the other table's key WHERE it IS NOT NULL), along
    `link` ((mine, theirs): the relationship chosen), else the first one."""
    mine, theirs = link or keyed_to(schema, table)[other]
    return f"{table}.{mine}", known_in([(table, mine, other, theirs)], has)


def known_in(chain, has: bool) -> dict:
    """The first table's key [NOT] IN the chain's records whose key to it is known (a NULL key never defeats NOT IN)."""
    last = chain[-1]
    cond = chained(chain, lambda a: {"type": "null_check", "expression": {"type": "column", "source": a,
                                                                         "reference": last[3]},
                                     "operator": "is_not_null"},
                   chain[0][0], lambda i, t: f"{t}_has", _sub)
    return cond | {"operator": "in" if has else "not_in"}


def rows_from(r: dict) -> str | None:
    """The request whose records this one starts from ("those": step 2's affected_rows or request_result)."""
    return r.get("waits_for") if r.get("input") in ("affected_rows", "request_result") else None


def state(prompt, requests, schema, sources, values, parts) -> dict:
    d = {t["definition"]["table_id"]: t["definition"] for t in schema}
    names = {f"{t}.{c['column_id']}": f"{x['sql_name']}.{c['sql_name']}" for t, x in d.items() for c in x["columns"]}
    reqs = {}
    for r in requests:
        rid, main = r["id"], main_table(requests, sources, r["id"])
        found = [{"words": v["text"], "stored_in": names[v["column"]]} for v in values
                 if v["request"] == rid and v.get("role") == "lookup" and v.get("column")]
        changes = [names[a["column"]] for a in parts.get(rid, {}).get("assignments", [])]
        reqs[rid] = {"text": r["text"], "statement": r["statement"],
                     **({"table": d[main]["sql_name"]} if main else {}),
                     **({"also_uses": [d[t]["sql_name"] for t in sources.get(rid, {}).get("reads", []) if t in d]}
                        if sources.get(rid, {}).get("reads") else {}),
                     **({"values": found} if found else {}), **({"changes": changes} if changes else {}),
                     **({"starts_from": f"the records request {rows_from(r)} chose or changed"}
                        if rows_from(r) else {})}
    return {"prompt": prompt, "requests": reqs,
            "tables": {x["sql_name"]: [c["sql_name"] for c in x["columns"]] for x in d.values()}}


def candidates(schema, requests, sources, rid) -> dict:
    """The columns asked about: every column of the request's tables but the keys linking them (foreign keys, and the
    read tables' own ids): the next steps join through them from the schema, as step 4 does with a linking table.
    The main table's own id stays ('delete the variant 109')."""
    main, d = main_table(requests, sources, rid), {t["definition"]["table_id"]: t["definition"] for t in schema}
    links = {f"{t}.{c}" for t, x in d.items() for fk in x.get("foreign_keys", []) for c in fk["columns"]}
    links |= {f"{t}.{c}" for t, x in d.items() if t != main for c in x.get("primary_key", [])}
    return {k: v for k, v in columns_of(schema, tables_of(requests, sources, rid)).items() if k not in links}


def tables_of(requests, sources, rid) -> list[str]:
    main = main_table(requests, sources, rid)
    return [main] + [t for t in sources.get(rid, {}).get("reads", []) if t != main] if main else []


def asked_columns(schema, requests, sources, rid, named, dated, db, units=None) -> dict:
    """The columns stage 1 asks about (step 1.4, 2026-10-28): the candidates the request names (step "columns":
    words, meaning, kind), its main table's own id ("delete the variant 109"), and, when it writes a date, the
    columns the profile says hold dates; when it writes more numbers than the number columns it names take ("nothing
    in stock and nothing reserved": two, only reserved named), the main table's other number columns; when it writes
    a number with a unit ("200 grams"), the columns whose own unit
    it converts to (2026-10-29: "weigh" named no column, the weight's condition was lost); a column step 3 tied to a
    value is kept without a question (build). Without the step's answer (named None): every candidate, as before."""
    every = candidates(schema, requests, sources, rid)
    if named is None:
        return every
    main = main_table(requests, sources, rid)
    own = {f"{main}.{c}" for t in schema if t["definition"]["table_id"] == main
           for c in t["definition"].get("primary_key", [])}
    def measured(k):                                          # both units known, one converts to the other
        info = column_info(schema, k)
        return (info["unit"] or {}).get("unit") and any((p.get("unit") or {}).get("unit") and fits(p, info) is not None
                                                        for p in (units or {}).get(rid, []))
    def numeric(k):                                           # a number column, no key
        t, c = k.split(".")
        d = next(x["definition"] for x in schema if x["definition"]["table_id"] == t)
        return column_info(schema, k)["sql_type"] in ("INTEGER", "REAL") and c not in d.get("primary_key", []) \
            and not any(c in fk["columns"] for fk in d.get("foreign_keys", []))
    days = [(d["start"], d["end"]) for d in (dated or {}).get(rid, []) if "start" in d]
    written = [p for p in (units or {}).get(rid, []) if p.get("value") is not None and "start" in p
               and not any(a < p["end"] and p["start"] < b for a, b in days)]   # a date's numbers are the date's
    short = len(written) > sum(numeric(k) for k in (named.get(rid) or {}) if k in every)   # more numbers than the
    return {k: v for k, v in every.items() if k in (named.get(rid) or {}) or k in own   # named number columns take:
            or (dated.get(rid) and holds_dates(db, schema, k)) or measured(k)       # the main table's others too
            or (short and k.split(".")[0] == main and numeric(k))}   # ("nothing in stock": 2026-10-30, Jev judges)


def questions(requests, schema, sources, named=None, dated=None, db=None, units=None) -> dict:
    q = {}
    for r in requests:
        rid, said = r["id"], f"request {r['id']} ('{r['text']}')"
        if r["statement"] not in ROWS or not main_table(requests, sources, rid):
            continue
        q[f"{rid}_all"] = yes_no(CONTEXT, f"Does {said} act on every record of its table, with no condition?",
                                 ALL_RULES)
        for key, text in asked_columns(schema, requests, sources, rid, named, dated or {}, db, units).items():
            q[f"{rid}_chooses_{key}"] = yes_no(CONTEXT, f"Does {said} keep or exclude records by what this column "
                                                        f"holds, or by its being empty: {text}", CHOOSE_RULES)
        names = {t["definition"]["table_id"]: t["definition"]["sql_name"] for t in schema}
        main = main_table(requests, sources, rid)
        for rel in relationships(schema, main) + paths(schema, main, tables_of(requests, sources, rid)):
            other, by = rel["other"], f" (by {rel['theirs'][2:]})" if "~" in rel["key"] else ""
            if rel.get("via"):
                by = f" through their {names[rel['via']['table']]}"
            q[f"{rid}_has_{rel['key']}"] = choice(CONTEXT, f"Does {said} keep its records by whether a record of "
                                                           f"{names[other]} matches them{by}?",
                                                  has_options(names[other]) | ({"compares": COMPARES.format(
                                                      names[other])} if rel["children"] else {}))
        if r["statement"] == "select":
            for m, text in MODIFIERS.items():
                q[f"{rid}_{m}"] = yes_no(CONTEXT, f"In {said}: {text}")
    return q


def comparable(a: str, b: str) -> bool:
    """Two columns a condition may compare: the same type, or both numbers (an INTEGER of whole prices, 99.00, against
    a REAL price)."""
    return a == b or {a, b} <= {"INTEGER", "REAL"}


def build(requests, schema, sources, values, answers) -> dict:
    out, warnings = {}, []

    def yes(rid, key, what):
        """Whether the column chooses the rows. Asked only when Jev leans yes without being sure: leaning no, the
        column is left out unasked, since a value only it could take is then used by no part of the request and the
        ledger refuses it (2026-11-01: 5 of 6 such questions, on set G, ended right with no)."""
        p = answers.get(key, 0.0)
        if 0.5 <= p < SURE:
            out[rid]["flagged"] = True
            warnings.append({"request": rid, "problem": f"{what} uncertain", "probability": round(p, 2)}
                            | question(answers, key))
        return p >= 0.5

    for r in requests:
        rid = r["id"]
        if r["statement"] not in ROWS or not main_table(requests, sources, rid):
            continue
        out[rid] = {}
        found = {v["column"] for v in values if v["request"] == rid and v.get("role") == "lookup" and v.get("column")}
        cols = []
        for key in columns_of(schema, tables_of(requests, sources, rid)):
            if key not in found and key not in candidates(schema, requests, sources, rid):
                continue
            if key in found:                                  # SQLite found the request's value there
                cols.append(key)
            elif yes(rid, f"{rid}_chooses_{key}", f"chooses by {key}"):
                cols.append(key)
        has, main = [], main_table(requests, sources, rid)
        for rel in relationships(schema, main) + paths(schema, main, tables_of(requests, sources, rid)):
            other, key = rel["other"], f"{rid}_has_{rel['key']}"   # a matching record there, none, how many / much
            c, probs = answers.get(key, (None, {}))
            if c in ("has", "has_not", "compares"):
                has.append({"table": other, "has": c != "has_not", "of": main, "link": [rel["mine"], rel["theirs"]],
                            "key": rel["key"], "sure": round(probs.get(c, 0), 2),
                            **({"compares": True} if c == "compares" else {}),
                            **({"via": rel["via"]} if rel.get("via") else {})})
            if c is not None and probs.get(c, 1) < SURE:
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"a matching record in {other} uncertain", "answer": c,
                                 "probability": round(probs.get(c, 0), 2)} | question(answers, key))
        all_rows = yes(rid, f"{rid}_all", "all rows")
        if all_rows and (cols or has):
            warnings.append({"request": rid, "problem": "every record, yet conditions found", "columns": cols,
                             **({"linked": [h["table"] for h in has]} if has else {})})
            out[rid]["flagged"] = True
        if not all_rows and not cols and not has and not rows_from(r):   # "those": its records come from the other request
            warnings.append({"request": rid, "problem": "some records, but no condition column found"})
            out[rid]["flagged"] = True
        out[rid] |= {"all_rows": all_rows and not cols and not has, "columns": cols, **({"linked": has} if has else {}),
                     **({"rows_from": rows_from(r)} if rows_from(r) else {})}
        if r["statement"] == "select":
            for m in MODIFIERS:
                out[rid][m] = yes(rid, f"{rid}_{m}", m)
    return {"requests": out, "warnings": warnings}


def _col(key):
    t, c = key.split(".")
    return {"type": "column", "source": t, "reference": c}


def _lit(v):
    return {"type": "literal", "value": v}


@functools.cache
def header_unit(header: str):
    """A header's unit (GLiNER2, ~0.2 s): the same header always declares the same unit, so it is read once per
    process (it was read 56 times per prompt)."""
    return declaration(header)


def column_info(schema, key) -> dict:
    t, c = key.split(".")
    col = next(x for x in next(d["definition"] for d in schema if d["definition"]["table_id"] == t)["columns"]
               if x["column_id"] == c)
    return col | {"unit": header_unit(col["header"] or col["sql_name"])}


def holds_dates(db, schema, key) -> bool:
    """The import's profile says the column holds dates (YYYY-MM-DD); a store never profiled: a text column may."""
    t, c = key.split(".")
    name = next(d["definition"]["sql_name"] for d in schema if d["definition"]["table_id"] == t)
    f = facts(db, name, column_info(schema, key)["sql_name"]) if db is not None else {}
    return f.get("dates", not f and column_info(schema, key)["sql_type"] == "TEXT")


def fits(param: dict, info: dict):
    """The number's value in the column's unit, or None when its unit is of another kind (a unit is converted only
    when both are known; a column unit unknown takes any number, unconverted: Jev chooses)."""
    if info["sql_type"] not in ("INTEGER", "REAL") or param["value"] is None:
        return None
    pu, cu = (param["unit"] or {}).get("unit"), (info["unit"] or {}).get("unit")
    if not pu or not cu or pu == cu:
        return param["value"]
    ureg = _tables()[0]
    try:
        return round(ureg.Quantity(param["value"], pu).to(cu).magnitude, 6)
    except Exception:                                         # another dimension: "1 kg" is not a price
        return None


def written(parts_req: dict) -> set:
    """The numbers step 5 writes as new values ("set the price to 199"): not a condition's operand."""
    out = set()

    def walk(n):
        if isinstance(n, dict):
            if n.get("type") == "literal" and isinstance(n.get("value"), (int, float)):
                out.add(float(n["value"]))
            for v in n.values():
                walk(v)
        elif isinstance(n, list):
            for v in n:
                walk(v)
    walk(parts_req.get("assignments", []))
    return out


def operands(db, schema, r, key, values, params, parts_req) -> dict:
    """{option id: (description, operand)} for one condition column."""
    info, out = column_info(schema, key), {}
    table = key.split(".")[0]
    for i, v in enumerate(v for v in values if v["request"] == r["id"] and v.get("column") == key):
        if v.get("match") == "given":                         # the user's column: its stored spelling there only
            found = [f for f in search(db, schema, v["text"], table) + within(db, schema, v["text"]) if f["column"] == key]
            by_meaning = False
            if not found:                                     # in another language ("variantes noires" → black):
                found = [f for f in meant(db, schema, v["text"]) if f["column"] == key][:1]   # the meaning, in that column
                by_meaning = bool(found)
            if not found and info["sql_type"] in ("INTEGER", "REAL") and \
                    not v["text"].replace(".", "", 1).lstrip("-").isdigit():
                continue                                      # "variant 106" on an id: its number is the operand
            stored = found[0]["stored"] if found else [v["text"]]
            if by_meaning:                                    # a meaning is not SQL's proof: the user confirms
                out[f"s{i}"] = (f"'{v['text']}' (meaning {', '.join(stored)})", stored, v["text"], "meaning")
                continue
        else:
            stored = v.get("stored") or [v["text"]]
        # + the text as written for a pattern: the part SQL matched ("Orbit" in "Orbit products"), else the phrase
        out[f"s{i}"] = (f"'{v['text']}' (stored as {', '.join(stored)})", stored, v.get("searched") or v["text"])
    used = written(parts_req)
    for i, p in enumerate(params):
        x = fits(p, info)
        if x is not None and float(p["value"]) not in used:
            unit = (info["unit"] or {}).get("unit")
            out[f"n{i}"] = (f"{p['text']}" + (f" = {x} {unit}" if unit and x != p["value"] else ""), x)
    if holds_dates(db, schema, key):
        out["today"] = (f"today, the session's date ({dates.today().isoformat()})", dates.today().isoformat())
        for i, d in enumerate(r.get("dates", [])):            # units/dates: a day, a whole period, a length
            v = d["value"]                                    # of time from today (as a day)
            if v["kind"] == "period":
                out[f"d{i}"] = (f"the period '{d['text']}' ({v['start']} to {v['end']}, the end excluded)",
                                {"type": "period", "start": v["start"], "end": v["end"]})
            elif v.get("date"):
                out[f"d{i}"] = (f"the date {v['date']} ('{d['text']}')", v["date"])
    elif info["sql_type"] == "TEXT":
        # texts for a pattern ("contains 100%"): step 3's phrases and the parameters as written, quoted text; never a
        # text already offered as a stored value (Jev would split its answer between the two)
        given = {x.casefold() for c in out.values() if isinstance(c[1], list) for x in c[1]}
        given |= {v["text"].casefold() for v in values if v["request"] == r["id"] and v.get("column") == key}
        pool = [v["text"] for v in values if v["request"] == r["id"] and v.get("role") in ("lookup", "written")]
        pool += [p["text"] for p in params] + [a or b for a, b in re.findall(r"'([^']+)'|\"([^\"]+)\"", r["text"])]
        phrases = {d["text"] for d in r.get("dates", [])}
        for i, t in enumerate(dict.fromkeys(t for t in pool if t.casefold() not in given and t not in phrases)):
            out[f"x{i}"] = (f"the text '{t}'", t)
    for other in columns_of(schema, [table]):
        if other != key and comparable(column_info(schema, other)["sql_type"], info["sql_type"]):
            out[f"c:{other}"] = (f"the {other.split('.')[1][2:]} column", _col(other))
    names = {x["definition"]["table_id"]: x["definition"]["sql_name"] for x in schema}
    for other in columns_of(schema, list(linked(schema, table))):   # a linked table's: "below the recommended price"
        if comparable((o := column_info(schema, other))["sql_type"], info["sql_type"]):
            out[f"c:{other}"] = (f"the {o['sql_name']} column of {names[other.split('.')[0]]}", _col(other))
    return out


def questions_2(db, requests, schema, stage1, values, params, parts) -> dict:
    q = {}
    for r in requests:
        rid, said = r["id"], f"request {r['id']} ('{r['text']}')"
        for key in stage1.get(rid, {}).get("columns", []):
            info = column_info(schema, key)
            name = f"the {info['sql_name']} column of {key.split('.')[0][2:]} ({info['sql_type']})"
            ops = {k: v for k, v in OPERATORS.items() if k not in TEXT_ONLY or info["sql_type"] == "TEXT" and not
                   holds_dates(db, schema, key)}             # a date column: no "contains"
            cands = operands(db, schema, r, key, values, params[rid], parts.get(rid, {}))
            opts = {k: c[0] for k, c in cands.items()}
            if len(several := mentions(cands)) > 1:           # one condition per value written
                for m in several:
                    with_ = f"{cands[m][0]} (in {around(r, key, values, params[rid], m)})"
                    q[f"{rid}|{key}|{m}|op"] = choice(
                        CONTEXT, f"In {said}, how does the condition on {name} with {with_} choose records?",
                        {k: v for k, v in ops.items() if k not in ONE} | {"none": "Not a condition on this column."},
                        MENTION_RULES)
                    q[f"{rid}|{key}|{m}|not"] = yes_no(CONTEXT, f"In {said}, is the condition on {name} with "
                                                                f"{with_} written as an exception or a negation?",
                                                       NEGATE_RULES + MENTION_RULES[-1:])
                continue
            q[f"{rid}|{key}|op"] = choice(CONTEXT, f"How does {said} choose its records by {name}?", ops)
            if any(k.startswith("s") for k in cands):         # step 3 proved the value with SQL: nothing to ask
                q[f"{rid}|{key}|not"] = yes_no(CONTEXT, f"In {said}, is the condition on {name} written as an "
                                                        f"exception or a negation?", NEGATE_RULES)
                continue
            q[f"{rid}|{key}|value"] = choice(CONTEXT, f"In {said}, which value is {name} compared with (the lower "
                                                      f"bound for a range)?", opts | {"computed": computed(),
                                                                                      "none": "None of these."})
            q[f"{rid}|{key}|upper"] = choice(CONTEXT, f"In {said}, if {name} is in a range, which value is its upper "
                                                      f"bound?", opts | {"none": "None of these, or not a range."})
            q[f"{rid}|{key}|not"] = yes_no(CONTEXT, f"In {said}, is the condition on {name} written as an exception "
                                                    f"or a negation?", NEGATE_RULES)
    return q


def predicate(op, col, operand, upper, all_values, text_type, amount=False):
    """The AST condition for one operator; None when an operand it needs is missing."""
    def esc(t):
        return str(t).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    one = operand[0] if isinstance(operand, list) and len(operand) == 1 else operand
    if any(isinstance(x, dict) and x.get("type") == "period" for x in (one, upper)):
        return period(op, col, one, upper)
    if op == "=" and isinstance(operand, list) and len(operand) > 1:   # one value, several spellings
        return {"type": "membership", "expression": col, "operator": "in", "values": [_lit(v) for v in operand]}
    if op in ("=", "<", "<=", ">", ">=") and one is not None:
        return {"type": "comparison", "operator": op, "left": col,
                "right": one if isinstance(one, dict) else _lit(one)}
    if op == "between" and one is not None and upper is not None:
        lo, hi = (x if isinstance(x, dict) else _lit(x[0] if isinstance(x, list) else x) for x in (one, upper))
        return {"type": "between", "expression": col, "lower": lo, "upper": hi, "negated": False}
    if op == "in" and all_values:
        return {"type": "membership", "expression": col, "operator": "in", "values": [_lit(v) for v in all_values]}
    if op in TEXT_ONLY and isinstance(one, str):
        pattern = {"contains": f"%{esc(one)}%", "starts_with": f"{esc(one)}%", "ends_with": f"%{esc(one)}"}[op]
        return {"type": "pattern", "expression": col, "pattern": pattern, "operator": "like", "escape": "\\"}
    if op in ("is_empty", "is_not_empty"):
        if text_type:
            return {"type": "empty_check", "expression": col, "operator": op, "policy": "null_or_space_only"}
        if amount:                                            # an amount's none is 0, as a text's is blank ("with a
            zero = {"type": "comparison", "operator": "!=" if op == "is_not_empty" else "=", "left": col,   # discount":
                    "right": _lit(0)}                         # its NULL test alone was always true, 2026-10-29)
            return zero if op == "is_not_empty" else {"type": "any", "children": [
                {"type": "null_check", "expression": col, "operator": "is_null"}, zero]}
        return {"type": "null_check", "expression": col, "operator": "is_null" if op == "is_empty" else "is_not_null"}
    return None


def period(op, col, one, upper):
    """A whole period (units/dates: its start, its end excluded) as the bounds it means: in it (=) from its start to
    its end; before it (<) before its start; after it (>) from its end; between two (a period or a day each) from the
    first's start to the second's end."""
    def cmp(o, v):
        return {"type": "comparison", "operator": o, "left": col, "right": _lit(v)}

    def low(x):
        return cmp(">=", x["start"] if isinstance(x, dict) else x)

    def high(x):
        return cmp("<", x["end"]) if isinstance(x, dict) else cmp("<=", x)
    if op == "between" and one is not None and upper is not None:
        return {"type": "all", "children": [low(one), high(upper)]}
    if not isinstance(one, dict):
        return None
    return {"=": {"type": "all", "children": [low(one), high(one)]}, "<": cmp("<", one["start"]),
            "<=": high(one), ">": cmp(">=", one["end"]), ">=": low(one)}.get(op)


def cased(cond, db, schema, key):
    """A text compared for equality with a value the column isn't known to hold (the profile lists none of its values,
    or not this one: a name, a SKU, "orbit hoodie" for Orbit Hoodie) is compared ignoring letter case; a stored
    spelling the profile knows stays exact (the user's decision, 2026-10-27, notice #12)."""
    t, c = key.split(".")
    name = next(d["definition"]["sql_name"] for d in schema if d["definition"]["table_id"] == t)
    known = {str(v) for v in listed(db, name, column_info(schema, key)["sql_name"])} if db is not None else set()

    def unknown(*nodes):
        return any(n.get("type") == "literal" and isinstance(n.get("value"), str) and n["value"] not in known
                   for n in nodes)
    if cond.get("type") == "comparison" and cond["operator"] in ("=", "!=") and unknown(cond["right"]):
        return cond | {"case": "ignore"}
    if cond.get("type") == "membership" and cond.get("values") and unknown(*cond["values"]):
        return cond | {"case": "ignore"}
    if cond.get("type") == "not":
        return cond | {"operand": cased(cond["operand"], db, schema, key)}
    return cond


def refused(db, schema, table, cond) -> str | None:
    """SQLite's error on a condition, prepared on its table (sql.check: no row read), or None."""
    stmt = {"id": "q", "type": "select", "from": {"type": "database_table", "reference": table, "alias": table},
            "columns": [{"expression": {"type": "aggregate", "function": "count"}, "alias": "n"}],
            "scope": {"type": "filtered", "where": cond}}
    try:
        return next((e["error"][:200] for e in check(db, program([stmt], schema))), None)
    except Exception as e:
        return str(e)[:200]


def mentioned(rid, key, m, cand, info, answers, out, warnings, db, schema, cands) -> dict | None:
    """One value's condition on a column (mentions()): its operator, its negation; None when it is not one."""
    def flag(problem, k=None, labels=None, p=None):         # k: the answer the user is asked again, if any
        out[rid]["flagged"] = True
        warnings.append({"request": rid, "problem": problem} | ({"probability": round(p, 2)} if p is not None else {})
                        | (question(answers, f"{rid}|{key}|{m}|{k}", labels) if k else {}))
    op, probs = answers.get(f"{rid}|{key}|{m}|op", (None, {}))
    if op not in (None, "none") and probs.get(op, 1) < SURE:
        flag(f"op of {key} with {cand[0]} uncertain", "op", {k: x[0] for k, x in cands.items()}, probs.get(op, 0))
    if op in (None, "none"):
        return None
    operand = cand[2] if op in TEXT_ONLY and len(cand) > 2 else cand[1]   # a pattern: the text as written
    if len(cand) > 3:                                         # a value read by its meaning: the user confirms
        flag(f"value read by its meaning: {cand[0]}, to confirm")
    cond = predicate(op, _col(key), operand, None, cand[1] if isinstance(cand[1], list) else [cand[1]],
                     info["sql_type"] == "TEXT")
    if cond and info["sql_type"] == "TEXT":
        cond = cased(cond, db, schema, key)
    cond = cond and through(schema, cond, key.split(".")[0])
    if cond is None:
        flag(f"condition on {key} with {cand[0]}: a part is missing")
        return None
    p = answers.get(f"{rid}|{key}|{m}|not", 0.0)
    if 1 - SURE < p < SURE:
        flag(f"negation of {key} with {cand[0]} uncertain", "not", p=p)
    if p >= 0.5:
        cond = {"type": "not", "operand": cond}
        if info["nullable"]:                                  # NOT (NULL = x) drops NULL rows
            cond = {"type": "any", "children": [
                {"type": "null_check", "expression": _col(key), "operator": "is_null"}, cond]}
    if err := refused(db, schema, key.split(".")[0], cond):
        flag(f"condition on {key} refused")
        warnings[-1]["error"] = err
    return {"column": key, "mention": m, "operator": op, "negated": p >= 0.5, "condition": cond}


def build_2(db, requests, schema, stage1, values, params, parts, answers, section) -> None:
    """Adds the predicates of stage 2 to stage 1's section. Two conditions are dropped, with their warnings (the
    request stays flagged only for what remains):
      - a text column with no value step 3 found in it, compared to another column or set equal to a text: SQL
        searched it and found nothing ("name = brand" when SQL found "Volt" in brand); a missing value stays flagged;
      - in a request that starts from another's records ("those"), a condition its own words give no value for:
        it is the other request's (r1's "heavier than 60 g" is not r2's).
    Two condition columns compared with each other ("recommended below our price") are one comparison: kept once,
    from the column whose operator Jev was surer of (read from both, it may come out reversed, and the two together
    would match nothing)."""
    out, warnings = section["requests"], section["warnings"]
    compared = {}                                             # (request, {column, column}) → (its predicate, sureness)

    def drop(rid, key, why):
        out[rid]["columns"] = [c for c in out[rid]["columns"] if c != key]
        warnings[:] = [w for w in warnings if not (w["request"] == rid and key in w["problem"])]
        if not any(w["request"] == rid and not w["problem"].startswith("no record matches") for w in warnings):
            out[rid].pop("flagged", None)
        out[rid].setdefault("dropped", []).append({"column": key, "why": why})

    for r in requests:
        rid = r["id"]
        for key in list(stage1.get(rid, {}).get("columns", [])):
            info = column_info(schema, key)
            cands = operands(db, schema, r, key, values, params[rid], parts.get(rid, {}))

            if len(several := mentions(cands)) > 1:           # one condition per value written
                for m in several:
                    built = mentioned(rid, key, m, cands[m], info, answers, out, warnings, db, schema, cands)
                    if built:
                        built["words"] = words_of(r, key, values, params[rid], [m], cands)
                        out[rid].setdefault("predicates", []).append(built)
                continue
            proved = [c for k, c in cands.items() if k.startswith("s")]   # step 3's values in this column (SQL)

            def pick(name, needed=True):
                c, probs = answers.get(f"{rid}|{key}|{name}", (None, {}))
                p = probs.get(c, 1)
                if name == "op" and proved and c in ("=", "in"):  # with SQL's values, both build the same condition
                    p = probs.get("=", 0) + probs.get("in", 0)
                if needed and c not in (None, "none") and p < SURE:
                    out[rid]["flagged"] = True
                    warnings.append({"request": rid, "problem": f"{name} of {key} uncertain", "answer": c,
                                     "probability": round(probs.get(c, 0), 2)}
                                    | question(answers, f"{rid}|{key}|{name}", {k: x[0] for k, x in cands.items()}))
                return c
            op = pick("op")
            for c in proved:
                if len(c) > 3:                                # a value read by its meaning in the user's column
                    out[rid]["flagged"] = True
                    warnings.append({"request": rid, "problem": f"value read by its meaning: {c[0]}, to confirm"})
            all_values = [x for c in proved for x in c[1]]
            needs_value = op not in ("in", "is_empty", "is_not_empty")
            if proved:                                        # the value is SQL's, never Jev's: = takes every spelling
                operand = proved[0][2] if op in TEXT_ONLY else all_values   # a pattern: the text as written
                upper = None
            else:
                v = pick("value", needs_value)
                if v == "computed":                           # stage 2b: an operation of the catalog
                    p = answers.get(f"{rid}|{key}|not", 0.0)
                    out[rid].setdefault("computed", []).append({"column": key, "operator": op, "negated": p >= 0.5})
                    continue
                u = pick("upper", op == "between")
                operand = cands[v][1] if needs_value and v in cands else None
                upper = cands[u][1] if op == "between" and u in cands else None
                # SQL searched this text column for the request's values and found none: comparing it to another
                # column, or setting it equal to a text, contradicts SQL. A missing value stays flagged ("in October").
                if info["sql_type"] == "TEXT" and (str(v).startswith("c:") or (op == "=" and str(v).startswith("x"))):
                    drop(rid, key, "contradicts SQL: step 3 found the request's values elsewhere")
                    continue
            other = None if proved else (str(v)[2:] if str(v).startswith("c:") else None)
            sure_op = answers.get(f"{rid}|{key}|op", (None, {}))[1].get(op, 0)
            if other and (seen := compared.get((rid, frozenset((key, other))))) and seen[1] >= sure_op:
                drop(rid, key, f"the comparison with {other}, built from that column")
                continue
            t, c = key.split(".")
            d = next(x["definition"] for x in schema if x["definition"]["table_id"] == t)
            keyed = c in d.get("primary_key", []) or any(c in fk["columns"] for fk in d.get("foreign_keys", []))
            cond = predicate(op, _col(key), operand, upper, all_values, info["sql_type"] == "TEXT",
                             info["sql_type"] in ("INTEGER", "REAL") and not keyed)   # a key's none is NULL only
            if cond and info["sql_type"] == "TEXT":
                cond = cased(cond, db, schema, key)
            cond = cond and through(schema, cond, key.split(".")[0])   # a linked table's column: through the keys
            if cond is None and rows_from(r):                    # the other request's condition
                drop(rid, key, f"the condition of request {rows_from(r)}, whose records this one starts from")
                continue
            p = answers.get(f"{rid}|{key}|not", 0.0)
            negated = p >= 0.5
            if 1 - SURE < p < SURE:
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"negation of {key} uncertain", "probability": round(p, 2)}
                                | question(answers, f"{rid}|{key}|not"))
            if cond is None:
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"condition on {key}: a part is missing", "operator": op,
                                 **({"text": r["text"]} if op in ("<", ">", "<=", ">=", "between") else {})})
                continue
            if negated:
                cond = {"type": "not", "operand": cond}
                if info["nullable"] and op not in ("is_empty", "is_not_empty"):   # NOT (NULL = x) drops NULL rows
                    cond = {"type": "any", "children": [
                        {"type": "null_check", "expression": _col(key), "operator": "is_null"}, cond]}
            if err := refused(db, schema, key.split(".")[0], cond):
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"condition on {key} refused", "error": err})
            built = {"column": key, "operator": op, "negated": negated, "condition": cond,
                     "words": words_of(r, key, values, params[rid],
                                       [k for k in cands if k.startswith("s")] if proved else [v, u], cands)
                     + (none_words(r, key, params[rid], section.get("named")) if op in ("is_empty", "is_not_empty")
                        else [])}
            if op in ("is_empty", "is_not_empty"):              # where it is written, for grouping ("with a discount")
                built["at"] = named_at(r, key, section.get("named"))
            if other and (seen := compared.get((rid, frozenset((key, other))))):   # the surer side replaces it
                out[rid]["predicates"][out[rid]["predicates"].index(seen[0])] = built
                out[rid]["columns"] = [c for c in out[rid]["columns"] if c != seen[0]["column"]]
            else:
                out[rid].setdefault("predicates", []).append(built)
            if other:
                compared[(rid, frozenset((key, other)))] = (built, sure_op)
        for h in stage1.get(rid, {}).get("linked", []):        # a matching record in a linked table, or none
            if h.get("compares"):
                continue                                       # how many / how much: stage 2c (build_2b)
            key, cond = existence_via(h["of"], h["link"][0], h["via"], h["table"], h["link"][1], h["has"]) \
                if h.get("via") else existence(schema, h["of"], h["table"], h["has"], h.get("link"))
            if err := refused(db, schema, key.split(".")[0], cond):
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"matching record in {h['table']} refused", "error": err})
            out[rid].setdefault("predicates", []).append({"column": key, "operator": "in" if h["has"] else "not_in",
                                                          "negated": False, "condition": cond, "linked": h["table"]})
        g = out.get(rid, {})
        if g and g.get("dropped") and not g["columns"] and not g.get("all_rows") and not rows_from(r):
            g["flagged"] = True                              # never widen to every record silently
            warnings.append({"request": rid, "problem": "some records, but no condition left after dropping",
                             "dropped": [d["column"] for d in g["dropped"]]})


def kind_of(db, schema, key) -> str:
    """A column's kind for the catalog's search (operations/use.py): number, date (the profile's), text."""
    if column_info(schema, key)["sql_type"] in ("INTEGER", "REAL"):
        return "number"
    return "date" if holds_dates(db, schema, key) else "text"


_CATALOG = {}
NUMBERS = ("percentage", "number")            # an entry's parameters a request's number fills
def words_of(r, key, values, params, keys, cands=None) -> list[list[int]]:
    """The request's words the chosen options stand for (positions in the request), for the ledger: a number (n), a
    date (d), a stored value of step 3 in this column (s), a factor word (w), a text as written (x)."""
    mine = [v for v in values if v["request"] == r["id"] and v.get("column") == key]
    facts = [x for x in find(r["text"]) if x["kind"] == "multiplier" or x["form"] in ("fraction", "mixed_number")]
    out = []
    for k in map(str, keys):
        i = int(k[1:]) if k[1:].isdigit() else None
        if i is None:
            continue
        if k[0] == "n" and i < len(params) and "start" in params[i]:
            out.append([params[i]["start"], params[i]["end"]])
        elif k[0] == "d" and i < len(r.get("dates", [])):
            out.append([r["dates"][i]["start"], r["dates"][i]["end"]])
        elif k[0] == "s" and i < len(mine) and "start" in mine[i]:
            out.append([mine[i]["start"] - r.get("start", 0), mine[i]["end"] - r.get("start", 0)])
        elif k[0] == "w" and i < len(facts):
            out.append([facts[i]["start"], facts[i]["end"]])
        elif k[0] == "x" and cands and k in cands and (j := r["text"].find(str(cands[k][1]))) >= 0:
            out.append([j, j + len(str(cands[k][1]))])
    return out


def named_at(r, key, named) -> list[list[int]]:
    """Where the column finder's words for this column are in the request (a place, not evidence: the ledger skips it)."""
    from orbitrows.pipeline.expressions.words import nlp
    own = {w.casefold() for p in (((named or {}).get(r["id"]) or {}).get(key) or {}).get("words") or []
           for w in str(p).split()}
    return [[t.idx, t.idx + len(t)] for t in nlp("en")(r["text"]) if t.text.casefold() in own] if own else []


def none_words(r, key, params, named) -> list[list[int]]:
    """The words of none ("no", "nothing": the reader's) attached in the parse to this column's own words ("no barcode"):
    an empty test's evidence. One attached elsewhere ("no reviews and zero stock") is not this condition's: the ledger
    refuses it if nothing else uses it."""
    from orbitrows.pipeline.expressions.words import nlp
    own = {w.casefold() for p in (((named or {}).get(r["id"]) or {}).get(key) or {}).get("words") or []
           for w in str(p).split()}
    if not own:
        return []
    doc = nlp("en")(r["text"])
    out = []
    for p in params:
        t = next((t for t in doc if t.idx == p.get("start")), None) if p.get("none") else None
        if t is not None and (t.head.text.casefold() in own or t.text.casefold() in own):
            out.append([p["start"], p["end"]])
    return out


def factors(text: str) -> dict:
    """The multipliers and fractions a request writes ("twice", "two and a half times", "a third", "2.5x"), read by the
    shared reader (units/quantity), offered next to its numbers: {"w0": (value, "'twice' (2)")}; Jev chooses."""
    return {f"w{i}": (plain(q["value"]), f"'{q['text']}' ({plain(q['value'])})") for i, q in enumerate(
        x for x in find(text) if x["kind"] == "multiplier" or x["form"] in ("fraction", "mixed_number"))}


AGGREGATE = {"count": "How many of them there are ('more than 3 variants').",
             "count_distinct": "How many different values of one of their columns ('in more than 2 colors').",
             "sum": "The total of one of their columns ('whose orders total over 1000', 'stock under 10').",
             "avg": "The average of one of their columns ('whose variants average over 200').",
             "min": "The lowest value of one of their columns ('whose cheapest variant is under 100').",
             "max": "The highest value of one of their columns ('whose latest order is before 2026').",
             "all": "Whether every one of them meets the request's condition on them ('whose variants are all black', "
                    "'all priced under 150').",
             "none": "None of these."}
OPS = {"<": "lower than", "<=": "at most", ">": "greater than", ">=": "at least", "=": "exactly", "!=": "not exactly"}


def questions_2c(requests, schema, section, params) -> dict:
    """Stage 2c, per linked table whose records the request compares (stage 1's "compares"): the aggregate, the
    column of theirs it reads, the operator, the number; asked in stage 2b's call."""
    q, names = {}, {t["definition"]["table_id"]: t["definition"]["sql_name"] for t in schema}
    for r in requests:
        rid, said = r["id"], f"request {r['id']} ('{r['text']}')"
        for h in section.get(rid, {}).get("comparisons", []):
            k, other = f"{rid}|{h['key']}", names[h["table"]]
            q[f"{k}|aggregate"] = choice(CONTEXT, f"In {said}, what does it compare of the records of {other} linked to "
                                                  f"each record?", AGGREGATE)
            if not h.get("column"):                           # a column stage 1 chose: known
                q[f"{k}|column"] = choice(CONTEXT, f"In {said}, when it totals, averages, or takes the lowest or "
                                                   f"highest of a value of the {other} ('the stock': the quantity; "
                                                   f"'the cheapest': the price), which column of {other} holds that "
                                                   f"value?", columns_of(schema, [h["table"]]) | {
                                              "none": f"None: it only counts the {other}, or checks that all of them "
                                                      f"meet a condition."})
            q[f"{k}|op"] = choice(CONTEXT, f"In {said}, how is that compared with the number?", OPS)
            for c in on_table(section.get(rid, {}), h["table"]):   # a condition on those records: which ones count
                q[f"{k}|inside|{c['column']}"] = choice(
                    CONTEXT, f"In {said}, the condition on {c['column'].split('.')[1][2:]} of {other}: does it choose "
                             f"which of the {other} are counted or compared, or the records themselves?",
                    {"inside": f"Only the {other} that meet it are counted or compared ('more than 2 black {other}': "
                               f"the black ones; 'whose {other} are all black': each of them must be black).",
                     "outside": f"It is a condition of its own: the records have such {other}, whatever is counted "
                                f"('with a black one and more than 3 {other} in all')."})
            nums = {f"n{i}": p["text"] for i, p in enumerate(params.get(rid, [])) if p["value"] is not None}
            q[f"{k}|number"] = choice(CONTEXT, f"In {said}, which number is it compared with?",
                                      nums | {k2: d for k2, (_, d) in factors(r["text"]).items()} | {"none": "None."})
    return q


def on_table(g: dict, table: str) -> list[dict]:
    """The request's conditions on a column of `table` (stage 2's predicates, not a matching record's)."""
    return [c for c in g.get("predicates", []) if c["column"].split(".")[0] == table and not c.get("linked")]


def comparisons(schema, main, g: dict) -> list[dict]:
    """The comparisons over linked records of one request: stage 1's "compares" ({"key", "table", "of", "link"}),
    and a computed condition stage 1 found on a column of a table whose records point to the main one ("whose
    variants average over 200": the price, of the variants), its column then known ({"column"})."""
    out = [h for h in g.get("linked", []) if h.get("compares")]
    for c in g.get("computed", []):
        table = c["column"].split(".")[0]
        rel = next((x for x in relationships(schema, main) if x["children"] and x["other"] == table), None) \
            if main and table != main else None
        if rel and not any(h["table"] == table for h in out):
            out.append({"key": c["column"], "table": table, "of": main, "link": [rel["mine"], rel["theirs"]],
                        "column": c["column"], "from_computed": True})
    return out


def build_2c(db, requests, schema, section, params, answers) -> None:
    """Each comparison over linked records built by the catalog's linked aggregate; below SURE a question; a part
    missing or unfit (a total of a text column): the request is refused, never run without the condition."""
    from orbitrows.pipeline.operations.builders import BUILDERS
    out, warnings = section["requests"], section["warnings"]
    for r in requests:
        rid = r["id"]
        found = out.get(rid, {}).pop("comparisons", [])
        said_as = {h["key"]: (answers.get(f"{rid}|{h['key']}|op", (None,))[0],
                              answers.get(f"{rid}|{h['key']}|number", (None,))[0]) for h in found}
        for h in list(found):                                 # one comparison read on two links ("suppliers whose
            twin = next((x for x in found if x is not h and said_as[x["key"]] == said_as[h["key"]]   # products have
                         and None not in said_as[h["key"]]), None)                          # more than 4 variants")
            if twin and (twin.get("sure", 0), -found.index(twin)) > (h.get("sure", 0), -found.index(h)):
                found.remove(h)
                out[rid].setdefault("dropped", []).append({"column": h["table"], "why": f"the same comparison, read "
                                                                                        f"over {twin['table']}"})
        for h in found:
            k, said = f"{rid}|{h['key']}", f"the comparison over the records of {h['table']}"

            def pick(name):
                v, probs = answers.get(f"{k}|{name}", (None, {}))
                if v is not None and probs.get(v, 1) < SURE:
                    out[rid]["flagged"] = True
                    warnings.append({"request": rid, "problem": f"{name} of {said} uncertain", "answer": v,
                                     "probability": round(probs.get(v, 0), 2)} | question(answers, f"{k}|{name}"))
                return None if v == "none" else v
            if "error" in answers:                            # the model's call failed: said as such
                out[rid]["refused"] = f"{said}: the comparison could not be read (model error), nothing is guessed"
                warnings.append({"request": rid, "problem": out[rid]["refused"], "error": answers["error"]})
                continue
            f, column, op, n = pick("aggregate"), h.get("column") or pick("column"), pick("op"), pick("number")
            typ = column and column_info(schema, column)["sql_type"]
            words = factors(r["text"])
            number = words[n][0] if n in words else params[rid][int(n[1:])]["value"] \
                if n and n[1:].isdigit() and int(n[1:]) < len(params.get(rid, [])) else None
            why = ("no aggregate fits" if f is None else
                   f"its {f} needs a column of {h['table']}" if f not in ("count", "all") and not column else
                   f"a {f} of the text column {column}" if f in ("sum", "avg") and typ not in ("INTEGER", "REAL") else
                   "no number to compare with" if number is None and f != "all" else
                   "no comparison" if op is None and f != "all" else None)
            for c in on_table(out[rid], h["table"]):          # the comparison itself, read again from a column
                right = (c["condition"].get("right") or {}).get("value")   # ("total stock under 20": quantity < 20,
                if f != "all" and c["operator"] == op and right == number:   # reserved < 20): kept once; for
                    out[rid]["predicates"].remove(c)                          # "all" it is the condition itself
            within = [c for c in on_table(out[rid], h["table"]) if pick(f"inside|{c['column']}") == "inside"]
            if f == "all" and not within:
                why = "'all' with no condition on the records it reads"
            if why:
                out[rid]["refused"] = f"{said}: {why}"
                warnings.append({"request": rid, "problem": f"not supported: {out[rid]['refused']}"})
                continue
            for c in within:                                  # read inside the aggregate, not a condition of its own
                out[rid]["predicates"].remove(c)
            p = {"column": _col(f"{h['of']}.{h['link'][0]}"), "function": f, "operator": op or "=", "number":
                 number if number is not None else 0,
                 "linked": {"table": h["table"], "key": h["link"][1], **({"value": column.split(".")[1]}
                                                                       if column and f not in ("count", "all") else {}),
                            **({"through": h["via"]} if h.get("via") else {})},
                 **({"where": within[0]["condition"] if len(within) == 1 else
                     {"type": "all", "children": [c["condition"] for c in within]}} if within else {})}
            cond = BUILDERS["linked_aggregate"](p, {})
            if err := refused(db, schema, h["of"], cond):
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"{said} refused", "error": err})
            out[rid].setdefault("predicates", []).append({"column": f"{h['of']}.{h['link'][0]}", "operator": op,
                                                          "negated": False, "condition": cond, "linked": h["table"],
                                                          "operation": "COMPARE_LINKED_AGGREGATE",
                                                          "words": words_of(r, None, [], params.get(rid, []), [n])
                                                          + [w for c in within for w in c.get("words", [])]})


GROUP_AGGREGATE = {"count": "How many records a group has ('the brands with more than 5 products').",
                   "count_distinct": "How many different values of a column a group has ('the warehouses holding "
                                     "fewer than 10 different variants').",
                   "sum": "The total of a column over a group ('the warehouses whose total stock is over 60').",
                   "avg": "The average of a column over a group ('the colors averaging over 150 in price').",
                   "min": "The lowest value of a column in a group.",
                   "max": "The highest value of a column in a group.",
                   "none": "None: every group is kept, or the request's conditions choose records, not groups."}


def questions_2d(requests, schema, section, params) -> dict:
    """Stage 2d, per grouped read (stage 1): does it keep only the groups whose records, together, compare with a
    number (HAVING), and if so the aggregate, the column of the main table it reads, the operator, the number; asked
    in stage 2b's call."""
    q = {}
    for r in requests:
        rid, said = r["id"], f"request {r['id']} ('{r['text']}')"
        if not (main := section.get(rid, {}).get("group_of")):
            continue
        k = f"{rid}|having"
        q[f"{k}|aggregate"] = choice(CONTEXT, f"{said} gives one answer per group. Does it keep only the groups whose "
                                              f"records, taken together, compare with a number? If so, what of them?",
                                     GROUP_AGGREGATE)
        q[f"{k}|column"] = choice(CONTEXT, f"In {said}, when a group's total, average, lowest, highest or number of "
                                           f"different values is compared, which column does it read?",
                                  columns_of(schema, [main]) | {"none": "None: it only counts the records."})
        q[f"{k}|op"] = choice(CONTEXT, f"In {said}, how is the group's figure compared with the number?", OPS)
        nums = {f"n{i}": p["text"] for i, p in enumerate(params.get(rid, [])) if p["value"] is not None}
        q[f"{k}|number"] = choice(CONTEXT, f"In {said}, which number is the group's figure compared with?",
                                  nums | {"none": "None."})
    return q


def in_full(schema, key, found) -> bool:
    """The request writes every word of the column's name (its unit aside): "quantity", "unit price", not the "mad"
    alone of cost_mad (the column finder's word matches)."""
    from .columns import name_words, unit_word
    t, c = key.split(".")
    d = next(x["definition"] for x in schema if x["definition"]["table_id"] == t)
    sql = next(x["sql_name"] for x in d["columns"] if x["column_id"] == c)
    own = set(name_words(sql, set(name_words(d["sql_name"], set())))) - {unit_word(sql)}
    return (found or {}).get("how") == "word" and bool(own) and own <= set(found.get("words") or [])


def build_2d(requests, schema, section, params, answers, named=None) -> None:
    """Each group condition: AGG(column) OP number, kept as the read's `having` (stage 4 passes it on); below SURE a
    question; a part missing or unfit: the request is refused, never run on every group. A condition stage 2 read
    from the same comparison on each record ("total stock over 60": quantity > 60) is that one: removed."""
    out, warnings = section["requests"], section["warnings"]
    for r in requests:
        rid = r["id"]
        if not (main := out.get(rid, {}).pop("group_of", None)):
            continue
        k = f"{rid}|having"

        def pick(name):
            v, probs = answers.get(f"{k}|{name}", (None, {}))
            if v is not None and probs.get(v, 1) < SURE:
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"{name} of the condition on groups uncertain",
                                 "answer": v, "probability": round(probs.get(v, 0), 2)} | question(answers, f"{k}|{name}"))
            return None if v == "none" else v
        if "error" in answers:
            out[rid]["refused"] = "the condition on groups could not be read (model error), nothing is guessed"
            warnings.append({"request": rid, "problem": out[rid]["refused"], "error": answers["error"]})
            continue
        if (f := pick("aggregate")) is None:
            continue
        column, op, n = pick("column"), pick("op"), pick("number")
        number = params[rid][int(n[1:])]["value"] if n and n[1:].isdigit() and int(n[1:]) < len(params.get(rid, [])) \
            else None
        typ = column and column_info(schema, column)["sql_type"]
        why = (f"its {f} needs a column of the grouped table" if f != "count" and not column else
               f"a {f} of the text column {column}" if f in ("sum", "avg") and typ not in ("INTEGER", "REAL") else
               "no number to compare with" if number is None else "no comparison" if op is None else None)
        if why:
            out[rid]["refused"] = f"the condition on groups: {why}"
            warnings.append({"request": rid, "problem": f"not supported: {out[rid]['refused']}"})
            continue
        for c in [c for c in out[rid].get("predicates", []) if not c.get("linked") and c["operator"] == op
                  and (c["condition"].get("right") or {}).get("value") == number]:
            out[rid]["predicates"].remove(c)
        others = [k for k, v in ((named or {}).get(rid) or {}).items() if k != column and k.split(".")[0] ==
                  column.split(".")[0] and column_info(schema, k)["sql_type"] in ("INTEGER", "REAL")
                  and in_full(schema, k, v)] if column else []
        if column and out[rid].get("refused_column") == column and not others:   # the same comparison read on each
            out[rid].pop("refused"); out[rid].pop("refused_column")   # record ("total stock over 60"): the group's;
            # with another number column of that table named in full ("quantity times unit price minus discount"),
            # the computation is over several columns, not this total: the refusal stays (hard set H10, 2026-10-31)
        agg = {"type": "aggregate", "function": "count" if f == "count_distinct" else f,
               "distinct": f == "count_distinct", **({"argument": _col(column)} if column and f != "count" else {})}
        out[rid]["having"] = {"type": "comparison", "operator": op, "left": agg,
                              "right": {"type": "literal", "value": number}}
        out[rid]["having_words"] = words_of(r, None, [], params.get(rid, []), [n])


def questions_2b(requests, schema, section, params, db=None) -> dict:
    """Per computed condition (build_2: Jev's value "computed"): which catalog operation (or none), which other
    column of its table it is computed from, which of the request's numbers it uses; one call."""
    from orbitrows.pipeline.operations import use
    from orbitrows.pipeline.operations.selector import described
    q, entries = {}, use.catalog()["entries"]
    for r in requests:
        rid, said = r["id"], f"request {r['id']} ('{r['text']}')"
        for c in section.get(rid, {}).get("computed", []):
            key = c["column"]
            info, ids = column_info(schema, key), use.search(r["text"], "condition", kind_of(db, schema, key))
            name = f"the {info['sql_name']} column of {key.split('.')[0][2:]}"
            q[f"{rid}|{key}|operation"] = choice(
                CONTEXT, f"In {said}, which operation computes the value {name} is compared with?",
                {i: described(entries[i]) for i in ids} | {"none": "None of these."}, COMPUTED_RULES)
            others = {o: f"the {o.split('.')[1][2:]} column" for o in columns_of(schema, [key.split(".")[0]])
                      if o != key and comparable(column_info(schema, o)["sql_type"], info["sql_type"])}
            q[f"{rid}|{key}|other"] = choice(CONTEXT, f"In {said}, which other column is that value computed from?",
                                             others | {"none": f"None: it is computed from {name} alone, or from "
                                                               f"no column."})
            nums = {f"n{i}": p["text"] for i, p in enumerate(params.get(rid, [])) if p["value"] is not None}
            q[f"{rid}|{key}|number"] = choice(CONTEXT, f"In {said}, which number does that computation use (a "
                                                       f"percentage, a factor, an amount, a number of days)?",
                                              nums | {k: d for k, (_, d) in factors(r["text"]).items()} |
                                              {"none": "None."})
        computed = [c["column"] for c in section.get(rid, {}).get("computed", [])]
        for a, b in ((a, b) for i, a in enumerate(computed) for b in computed[i + 1:]):   # read from both sides
            na, nb = (x.split(".")[1][2:] for x in (a, b))
            q[f"{rid}|{a}~{b}|compared"] = choice(
                CONTEXT, f"In {said}, which column is compared with a value computed from the other?",
                {a: f"the {na} is compared with a value computed from the {nb}",
                 b: f"the {nb} is compared with a value computed from the {na}"})
    return q


def questions_2w(requests, schema, section, answers) -> dict:
    """A computed condition read from a window (the 3 cheapest of each product, above the previous sale; step 2.2):
    its groups, its order and its direction, asked only for those (their own call, after stage 2b's)."""
    from orbitrows.pipeline.operations import use
    q, entries = {}, use.catalog()["entries"]
    for r in requests:
        rid, said = r["id"], f"request {r['id']} ('{r['text']}')"
        for c in section.get(rid, {}).get("computed", []):
            key = c["column"]
            e = entries.get(answers.get(f"{rid}|{key}|operation", (None,))[0])
            if not e or e["builder_key"] != "window_filter":
                continue
            declared = {x["name"] for x in e["parameters"]}
            cols, what = columns_of(schema, [key.split(".")[0]]), f"{e['name'].lower()} by {key.split('.c_')[-1]}"
            q[f"{rid}|{key}|within"] = choice(CONTEXT, f"In {said}, {what}: within which groups (the records sharing "
                                                       f"this column's value)?", {k: f"the {k.split('.c_')[-1]} "
                                                                                     f"column" for k in cols
                                                                                  if k != key}
                                              | {"none": "None: among all the records together."})
            if "order" in declared:
                q[f"{rid}|{key}|order"] = choice(CONTEXT, f"In {said}, {what}: which column puts the records in "
                                                          f"order (what 'previous' follows)?",
                                                 {k: f"the {k.split('.c_')[-1]} column" for k in cols}
                                                 | {"none": "None is said."})
            if "direction" in declared:
                q[f"{rid}|{key}|direction"] = choice(CONTEXT, f"In {said}, {what}: which records come first?",
                                                     {"asc": "The lowest or the earliest values ('the cheapest').",
                                                      "desc": "The highest or the latest values ('the dearest', "
                                                              "'the most recent')."})
    return q


def build_2b(db, requests, schema, section, params, answers, more=None) -> None:
    """Each computed condition built by its operation's builder, from the answers of questions_2b; below SURE a
    question; none, or an operation whose parameters are not all known: the request is refused (assembly does
    not build it: never run without one of its conditions)."""
    from orbitrows.pipeline.operations import use
    out, warnings = section["requests"], section["warnings"]
    for r in requests:
        rid, made = r["id"], []
        for c in out.get(rid, {}).pop("computed", []):
            key, said = c["column"], f"the condition on {c['column']}"
            heard = []                                        # its uncertain answers: questions if it is kept

            def pick(name, labels=None):
                src = more if more and f"{rid}|{key}|{name}" in more else answers   # more: the window's call
                v, probs = src.get(f"{rid}|{key}|{name}", (None, {}))
                if v is not None and probs.get(v, 1) < SURE:
                    heard.append({"request": rid, "problem": f"{name} of {said} uncertain", "answer": v,
                                  "probability": round(probs.get(v, 0), 2)}
                                 | question(src, f"{rid}|{key}|{name}", labels))
                return v, probs.get(v, 0)
            entries = use.catalog()["entries"]
            (op_id, sure), (other, _), (n, _) = (pick("operation", {i: e["name"] for i, e in entries.items()}),
                                                 pick("other"), pick("number"))
            entry = entries.get(op_id)
            given = {"column": _col(key), "operator": c["operator"]}   # the entry filled from its declaration
            if other not in (None, "none"):
                given["other"] = _col(other)
            if entry and entry["builder_key"] == "window_filter":   # each record found again by its own id
                table = key.split(".")[0]
                pk = next((f"{table}.{x}" for t in schema if t["definition"]["table_id"] == table
                           for x in t["definition"].get("primary_key", [])), None)
                (within, _), (order, _), (way, _) = pick("within"), pick("order"), pick("direction")
                given |= {"key": pk and _col(pk), "direction": way} | {
                    k: _col(v) for k, v in (("within", within), ("order", order)) if v not in (None, "none")}
                if "order" not in given and any(x["name"] == "order" for x in entry["parameters"]) and pk:
                    given["order"] = _col(pk)                 # "previous" with no order said: the records' own
                if given.get("within") == given.get("order", given["column"]):
                    given.pop("within")                       # groups of equal values have nothing left to order
            words = factors(r["text"])
            if entry and n in words:
                f = words[n][0]
                given |= {x["name"]: f * 100 if x["type"] == "percentage" else f for x in use.slots(entry)
                          if x["type"] in NUMBERS}
            elif entry and n not in (None, "none") and n[1:].isdigit() and int(n[1:]) < len(params.get(rid, [])):
                given |= {x["name"]: params[rid][int(n[1:])]["value"] for x in use.slots(entry) if x["type"] in NUMBERS}
            p, missing = use.fill(entry, given) if entry else ({}, [])
            made.append({"c": c, "entry": entry, "p": p, "missing": missing, "sure": sure, "heard": heard,
                         "other": other, "words": words_of(r, key, [], params.get(rid, []), [n])})
        # two columns computed from each other are one condition ("priced more than twice their cost" is read from
        # the price and from the cost): the one that builds, the surer, is kept
        for i, m in enumerate(made):
            for k, x in enumerate(made):
                if k == i or not (x["c"]["column"] == m["other"] or x["other"] == m["c"]["column"]):
                    continue
                a, b = (m, x) if i < k else (x, m)
                said_by = answers.get(f"{rid}|{a['c']['column']}~{b['c']['column']}|compared", (None, {}))[0]
                built = lambda y: bool(y["entry"]) and not y["missing"]
                if said_by in (a["c"]["column"], b["c"]["column"]) and built(a) == built(b):   # the subject, as
                    m["dropped"] = said_by != m["c"]["column"]                                # Jev reads it
                elif built(m) != built(x):                     # only one of the two readings can be built: that one
                    m["dropped"] = not built(m)
                else:                                          # no answer: the surer, the first
                    m["dropped"] = m.get("dropped") or (x["sure"], -k) > (m["sure"], -i)
        for m in made:
            c, entry, key = m["c"], m["entry"], m["c"]["column"]
            said = f"the condition on {key}"
            if m.get("dropped"):
                out[rid]["columns"] = [x for x in out[rid]["columns"] if x != key]
                out[rid].setdefault("dropped", []).append({"column": key, "why": "computed from the other column, "
                                                                                 "built from that one"})
                continue
            if m["heard"]:
                out[rid]["flagged"] = True
                warnings.extend(m["heard"])
            if entry is None and "error" in answers:          # the model's call failed: said as such
                out[rid]["refused"] = f"{said}: the operation could not be chosen (model error), nothing is guessed"
                warnings.append({"request": rid, "problem": out[rid]["refused"], "error": answers["error"]})
                continue
            if entry is None:                                  # none: honest, never guessed
                out[rid]["refused_column"] = key
                out[rid]["refused"] = f"{said} compares with a computation no operation supports yet"
                warnings.append({"request": rid, "problem": f"not supported: {out[rid]['refused']}"})
                continue
            if m["missing"]:
                out[rid]["refused_column"] = key
                out[rid]["refused"] = f"{said} ({entry['name']}) lacks its parameters: {'; '.join(m['missing'])}"
                warnings.append({"request": rid, "problem": f"invalid parameters: {out[rid]['refused']}"})
                continue
            cond = through(schema, use.build(entry, m["p"]), key.split(".")[0])
            if c["negated"]:
                cond = {"type": "not", "operand": cond}
            if err := refused(db, schema, key.split(".")[0], cond):
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"condition on {key} refused", "error": err})
            out[rid].setdefault("predicates", []).append({"column": key, "operator": c["operator"],
                                                          "negated": c["negated"], "condition": cond,
                                                          "operation": entry["id"], "words": m["words"]}
                                                         | windowed(entry, m["p"]))


def windowed(entry, p) -> dict:
    """A window condition's groups and ranking, for the grouping step: "of each product" and "the 2 dearest" are its
    groups and its number, never a GROUP BY or a LIMIT again (step 2.2)."""
    if entry["builder_key"] != "window_filter":
        return {}
    w = p.get("within")
    return {"window": {"within": w and f"{w['source']}.{w['reference']}",
                       "ranked": entry["config"]["function"] == "rank"}}


def dated(r, found, answers, section) -> list[dict]:
    """The request's dates read (units/dates), each {"text", "start", "end", "value"}; an unsure classification is a
    question, and that date is not offered; a date the reader refuses refuses the request (never run without it)."""
    out = []
    for i, d in enumerate(found):
        v = dates.read(d["text"], answers, f"{r['id']}|t{i}")
        if v is None and answers.get(f"{r['id']}|t{i}|kind", (None,))[0] not in (None, "none") and \
                r["id"] in section["requests"]:
            section["requests"][r["id"]]["refused"] = f"'{d['text']}' can't be read as a date (the date reader " \
                                                      f"refuses it)"
        if v and "ask" in v:
            a = v["ask"]
            section["requests"].setdefault(r["id"], {})["flagged"] = True
            section["warnings"].append({"request": r["id"], "problem": a["problem"], "answer": a["answer"],
                                        "probability": a["probability"]} | question(answers, a["key"]))
        elif v:
            out.append(d | {"value": v} | ({"text": d["text"][:v["length"]], "end": d["start"] + v["length"]}
                                           if "length" in v else {}))
    return out


def read_dates(requests, found, answers, section) -> list[dict]:
    """Each request with its dates read ("dates"); with the date reader's service down, a request that writes a date
    is refused (never run without its date condition), the others go on."""
    try:
        return [r | {"dates": dated(r, found.get(r["id"], []), answers, section)} for r in requests]
    except dates.EngineUnavailable:
        for rid, spans in found.items():
            if spans and rid in section["requests"]:
                section["requests"][rid]["refused"] = f"its dates can't be read: {dates.DOWN}"
        return [r | {"dates": []} for r in requests]


async def run(jev, prompt, requests, schema, sources: dict, values: list[dict], parts: dict, db=None, model=None,
              named=None) -> dict:
    """sources: step 4's section["requests"]; values: step 3's section["values"]; parts: step 5's
    section["requests"]."""
    """db, model: the store and GLiNER2, for stage 2 (without them, stage 1 only)."""
    st, a2 = state(prompt, requests, schema, sources, values, parts), {}
    found = {r["id"]: dates.spans(r["text"], model) for r in requests if r["statement"] in ROWS} if model else {}
    try:                                                      # their classification: in this call
        timed = {k: q for r in requests for i, d in enumerate(found.get(r["id"], [])) for k, q in dates.questions(
            f"{r['id']}|t{i}", f"request {r['id']} ('{r['text']}')", d["text"]).items()}
    except dates.EngineUnavailable:                           # read_dates refuses those requests
        timed = {}
    params = {r["id"]: parameters(r["text"], model) for r in requests} if db is not None and model is not None \
        else {}                                               # read first: a number's unit names its column
    try:
        answers = await ask(jev, st, questions(requests, schema, sources, named, found, db, params) | timed)
    except Exception as e:                                    # API error: nothing is guessed
        answers = {"error": repr(e)}
    section = build(requests, schema, sources, values, answers)
    requests = read_dates(requests, found, answers, section)
    for r in requests:                                        # kept for the formula ("set the date to …", "2 weeks
        if r["dates"] and r["id"] in section["requests"]:    # later"): read once
            section["requests"][r["id"]]["dates"] = r["dates"]
    if db is not None and model is not None:
        try:
            a2 = await ask(jev, st, questions_2(db, requests, schema, section["requests"], values, params, parts))
        except Exception as e:
            a2 = {"error": repr(e)}
        section["named"] = named or {}                       # the column finder's words, for an empty test's evidence
        build_2(db, requests, schema, section["requests"], values, params, parts, a2, section)
        named_now = section.pop("named", None)
        a2b = {}
        for r in requests:                                    # stage 2c's comparisons over linked records; a
            g = section["requests"].get(r["id"]) or {}        # computed one among them is not stage 2b's
            if found := comparisons(schema, main_table(requests, sources, r["id"]), g):
                g["comparisons"] = found
                routed = {h["column"] for h in found if h.get("from_computed")}
                g["computed"] = [c for c in g.get("computed", []) if c["column"] not in routed]
            if g.get("grouped"):                              # stage 2d: a condition on its groups (HAVING)
                g["group_of"] = main_table(requests, sources, r["id"])
        q_n, negated = questions_2n(requests, schema, section["requests"])
        q_x, owned = questions_2x(requests, schema, section["requests"])
        if q := questions_2b(requests, schema, section["requests"], params, db) | \
                questions_2c(requests, schema, section["requests"], params) | \
                questions_2d(requests, schema, section["requests"], params) | q_n | q_x:   # 2b-2d, 2n, 2x: one call
            try:
                a2b = await ask(jev, st, q)
            except Exception as e:
                a2b = {"error": repr(e)}
        a2w = {}
        if q := questions_2w(requests, schema, section["requests"], a2b):
            try:
                a2w = await ask(jev, st, q)
            except Exception as e:                            # API error: nothing is guessed
                a2w = {"error": repr(e)}
        build_2n(section, negated, a2b)
        build_2x(section, owned, a2b)
        build_2b(db, requests, schema, section, params, a2b, a2w)
        build_2c(db, requests, schema, section, params, a2b)
        build_2d(requests, schema, section, params, a2b, named_now)
        for g in section["requests"].values():               # "of the Accessories category": its name's condition;
            ps = g.get("predicates", [])                      # a linked test no words wrote ("has a category") on the
            g["predicates"] = [p for p in ps if not (p.get("linked") and not p.get("words") and any(   # same table
                q is not p and q["column"].split(".")[0] == p["linked"] for q in ps))]   # widened an OR (2026-10-29)
        section["parameters"] = params
        section["jev"] = {"stage_1": answers, "stage_2": a2, "stage_2b": a2b} | ({"stage_2w": a2w} if a2w else {})
        return section
    section["jev"] = {"stage_1": answers, "stage_2": a2}
    return section
