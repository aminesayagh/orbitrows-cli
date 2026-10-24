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
          then each condition runs on its table in a savepoint: an error is flagged, 0 rows is a warning
    ponytail: relative dates ("last month", "in October") are not read: flagged; add with a session date

Section "conditions" of cases/<id>.json: {"requests": {"r1": {"all_rows": bool, "columns": ["t_x.c_y"], "sorted"?,
"limited"?, "grouped"?, "linked"?: [{"table", "has", "of"}], "predicates": [{"column", "operator", "negated", "condition": AST,
"rows", "linked"?}], "flagged"?}},
"warnings", "jev": {"stage_1", "stage_2"}}.
"""
import functools
import json
import re

from orbitrows.sql.sql import program
from orbitrows.pipeline.store.context import declaration
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

CONTEXT = ("A merchant wrote a prompt to edit or read their store database (tables and columns in `tables`). It was "
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
         "apart).",
    "<": "Lower than ('under 300', 'cheaper than', 'before').", "<=": "At most ('300 or less', 'up to').",
    ">": "Greater than ('over 300', 'heavier than', 'after').", ">=": "At least ('300 or more', 'from').",
    "between": "Between two bounds, both included ('between 200 and 500').",
    "in": "One of several values ('black or navy', 'red, blue or white').",
    "contains": "Contains a text.", "starts_with": "Starts with a text.", "ends_with": "Ends with a text.",
    "is_empty": "Has no value ('without a description', 'no barcode').",
    "is_not_empty": "Has a value ('with a barcode').",
}
TEXT_ONLY = {"contains", "starts_with", "ends_with"}
NEGATE_RULES = [
    "Answer yes only when words negate the condition: 'except the Kora ones', 'not black', 'other than', "
    "'excluding', 'all but'.",
    "Answer no when no word negates it, whatever the request then does with the records: 'delete the refunded "
    "sales' (refunded, not negated).",
    "Answer no for 'without a description' or 'no barcode': that is the condition 'is empty', not a negation.",
]
DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def has_options(other: str) -> dict:
    return {"has": f"Only those with a matching record in {other} ('already in {other}', 'that exist in {other}').",
            "has_not": f"Only those with no matching record in {other} ('not in {other} yet', 'missing from "
                       f"{other}', 'without any {other}').",
            "neither": "Neither: it does not choose by that (a value of that table, 'the Kora ones', is a condition "
                       "on its column)."}


def keyed_to(schema, table) -> dict:
    """{other table: (this table's column, its column)} for each table linked to `table` by a foreign key, either way
    (the first key when there are several)."""
    out = {}
    for t, x in {d["definition"]["table_id"]: d["definition"] for d in schema}.items():
        for fk in x.get("foreign_keys", []):
            (c,), ref = fk["columns"], fk["references"]
            if t == table and ref["table"] != table:
                out.setdefault(ref["table"], (c, ref["columns"][0]))
            elif ref["table"] == table and t != table:
                out.setdefault(t, (ref["columns"][0], c))
    return out


def linked(schema, table) -> dict:
    """{other table: chain of keys [(from table, column, to table, column)]} for each table a record of `table` has
    one record of: many-to-one keys (a variant's product), and a key the other table holds as its own primary key
    (one-to-one: an offer per variant SKU)."""
    d = {x["definition"]["table_id"]: x["definition"] for x in schema}
    steps = []
    for t, x in d.items():
        for fk in x.get("foreign_keys", []):
            (c,), ref = fk["columns"], fk["references"]
            steps.append((t, c, ref["table"], ref["columns"][0]))
            if x.get("primary_key") == [c]:
                steps.append((ref["table"], ref["columns"][0], t, c))
    out, todo = {table: []}, [table]
    while todo:
        t = todo.pop(0)
        for s in steps:
            if s[0] == t and s[2] not in out:
                out[s[2]] = out[t] + [s]
                todo.append(s[2])
    out.pop(table)
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
        aliases = [f"{s[2]}_l{n}{i}" for i, s in enumerate(chain)]
        inner = json.loads(json.dumps(cond).replace(f'"source": "{other}"', f'"source": "{aliases[-1]}"'))
        for i in range(len(chain) - 1, -1, -1):
            t_from, c_from, t_to, c_to = chain[i]
            inner = {"type": "membership", "operator": "in", "query": _sub(t_to, aliases[i], c_to, inner),
                     "expression": {"type": "column", "source": aliases[i - 1] if i else table, "reference": c_from}}
        cond = inner
    return cond


def existence(schema, table, other, has: bool) -> tuple[str, dict]:
    """(the key column, its condition): key [NOT] IN (SELECT the other table's key WHERE it IS NOT NULL)."""
    mine, theirs = keyed_to(schema, table)[other]
    a = f"{other}_has"
    known = {"type": "column", "source": a, "reference": theirs}
    return f"{table}.{mine}", {"type": "membership", "operator": "in" if has else "not_in", "expression": _col(
        f"{table}.{mine}"), "query": _sub(other, a, theirs, {"type": "null_check", "expression": known,
                                                             "operator": "is_not_null"})}


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


def questions(requests, schema, sources) -> dict:
    q = {}
    for r in requests:
        rid, said = r["id"], f"request {r['id']} ('{r['text']}')"
        if r["statement"] not in ROWS or not main_table(requests, sources, rid):
            continue
        q[f"{rid}_all"] = yes_no(CONTEXT, f"Does {said} act on every record of its table, with no condition?",
                                 ALL_RULES)
        for key, text in candidates(schema, requests, sources, rid).items():
            q[f"{rid}_chooses_{key}"] = yes_no(CONTEXT, f"Does {said} keep or exclude records by what this column "
                                                        f"holds, or by its being empty: {text}", CHOOSE_RULES)
        names = {t["definition"]["table_id"]: t["definition"]["sql_name"] for t in schema}
        for other in keyed_to(schema, main_table(requests, sources, rid)):
            q[f"{rid}_has_{other}"] = choice(CONTEXT, f"Does {said} keep its records by whether a record of "
                                                      f"{names[other]} matches them?", has_options(names[other]))
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
        p = answers.get(key, 0.0)
        if 1 - SURE < p < SURE:
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
        for other in keyed_to(schema, main):                  # a matching record there, or none
            c, probs = answers.get(f"{rid}_has_{other}", (None, {}))
            if c in ("has", "has_not"):
                has.append({"table": other, "has": c == "has", "of": main})
            if c is not None and probs.get(c, 1) < SURE:
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"a matching record in {other} uncertain", "answer": c,
                                 "probability": round(probs.get(c, 0), 2)} | question(answers, f"{rid}_has_{other}"))
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
        if v.get("match") == "given":                         # the merchant's column: its stored spelling there only
            found = [f for f in search(db, schema, v["text"], table) + within(db, schema, v["text"]) if f["column"] == key]
            by_meaning = False
            if not found:                                     # in another language ("variantes noires" → black):
                found = [f for f in meant(db, schema, v["text"]) if f["column"] == key][:1]   # the meaning, in that column
                by_meaning = bool(found)
            stored = found[0]["stored"] if found else [v["text"]]
            if by_meaning:                                    # a meaning is not SQL's proof: the merchant confirms
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
    if info["sql_type"] == "TEXT":
        for i, d in enumerate(DATE.findall(r["text"])):
            out[f"d{i}"] = (f"the date {d}", d)
        # texts for a pattern ("contains 100%"): step 3's phrases and the parameters as written, quoted text; never a
        # text already offered as a stored value (Jev would split its answer between the two)
        given = {x.casefold() for c in out.values() if isinstance(c[1], list) for x in c[1]}
        given |= {v["text"].casefold() for v in values if v["request"] == r["id"] and v.get("column") == key}
        pool = [v["text"] for v in values if v["request"] == r["id"] and v.get("role") in ("lookup", "written")]
        pool += [p["text"] for p in params] + [a or b for a, b in re.findall(r"'([^']+)'|\"([^\"]+)\"", r["text"])]
        for i, t in enumerate(dict.fromkeys(t for t in pool if t.casefold() not in given and not DATE.fullmatch(t))):
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
            ops = {k: v for k, v in OPERATORS.items() if info["sql_type"] == "TEXT" or k not in TEXT_ONLY}
            cands = operands(db, schema, r, key, values, params[rid], parts.get(rid, {}))
            opts = {k: c[0] for k, c in cands.items()}
            q[f"{rid}|{key}|op"] = choice(CONTEXT, f"How does {said} choose its records by {name}?", ops)
            if any(k.startswith("s") for k in cands):         # step 3 proved the value with SQL: nothing to ask
                q[f"{rid}|{key}|not"] = yes_no(CONTEXT, f"In {said}, is the condition on {name} written as an "
                                                        f"exception or a negation?", NEGATE_RULES)
                continue
            q[f"{rid}|{key}|value"] = choice(CONTEXT, f"In {said}, which value is {name} compared with (the lower "
                                                      f"bound for a range)?", opts | {"none": "None of these."})
            q[f"{rid}|{key}|upper"] = choice(CONTEXT, f"In {said}, if {name} is in a range, which value is its upper "
                                                      f"bound?", opts | {"none": "None of these, or not a range."})
            q[f"{rid}|{key}|not"] = yes_no(CONTEXT, f"In {said}, is the condition on {name} written as an exception "
                                                    f"or a negation?", NEGATE_RULES)
    return q


def predicate(op, col, operand, upper, all_values, text_type):
    """The AST condition for one operator; None when an operand it needs is missing."""
    def esc(t):
        return str(t).replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    one = operand[0] if isinstance(operand, list) and len(operand) == 1 else operand
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
        return {"type": "null_check", "expression": col, "operator": "is_null" if op == "is_empty" else "is_not_null"}
    return None


def count(db, schema, table, cond) -> tuple[int | None, str | None]:
    """The rows a condition selects on its table, in a savepoint (nothing kept); or the error."""
    stmt = {"id": "q", "type": "select", "from": {"type": "database_table", "reference": table, "alias": table},
            "columns": [{"expression": {"type": "aggregate", "function": "count"}, "alias": "n"}],
            "scope": {"type": "filtered", "where": cond}}
    try:
        return db.execute(program([stmt], schema)[0]).fetchone()[0], None
    except Exception as e:
        return None, str(e)[:200]


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
                if len(c) > 3:                                # a value read by its meaning in the merchant's column
                    out[rid]["flagged"] = True
                    warnings.append({"request": rid, "problem": f"value read by its meaning: {c[0]}, to confirm"})
            all_values = [x for c in proved for x in c[1]]
            needs_value = op not in ("in", "is_empty", "is_not_empty")
            if proved:                                        # the value is SQL's, never Jev's: = takes every spelling
                operand = proved[0][2] if op in TEXT_ONLY else all_values   # a pattern: the text as written
                upper = None
            else:
                v = pick("value", needs_value)
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
            cond = predicate(op, _col(key), operand, upper, all_values, info["sql_type"] == "TEXT")
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
            n, err = count(db, schema, key.split(".")[0], cond)
            if err:
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"condition on {key} refused", "error": err})
            elif n == 0:
                warnings.append({"request": rid, "problem": f"no record matches the condition on {key}"})
            built = {"column": key, "operator": op, "negated": negated, "condition": cond, "rows": n}
            if other and (seen := compared.get((rid, frozenset((key, other))))):   # the surer side replaces it
                out[rid]["predicates"][out[rid]["predicates"].index(seen[0])] = built
                out[rid]["columns"] = [c for c in out[rid]["columns"] if c != seen[0]["column"]]
            else:
                out[rid].setdefault("predicates", []).append(built)
            if other:
                compared[(rid, frozenset((key, other)))] = (built, sure_op)
        for h in stage1.get(rid, {}).get("linked", []):        # a matching record in a linked table, or none
            key, cond = existence(schema, h["of"], h["table"], h["has"])
            n, err = count(db, schema, key.split(".")[0], cond)
            if err:
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": f"matching record in {h['table']} refused", "error": err})
            elif n == 0:
                warnings.append({"request": rid, "problem": f"no record matches the condition on {h['table']}"})
            out[rid].setdefault("predicates", []).append({"column": key, "operator": "in" if h["has"] else "not_in",
                                                          "negated": False, "condition": cond, "rows": n,
                                                          "linked": h["table"]})
        g = out.get(rid, {})
        if g and g.get("dropped") and not g["columns"] and not g.get("all_rows") and not rows_from(r):
            g["flagged"] = True                              # never widen to every record silently
            warnings.append({"request": rid, "problem": "some records, but no condition left after dropping",
                             "dropped": [d["column"] for d in g["dropped"]]})


async def run(jev, prompt, requests, schema, sources: dict, values: list[dict], parts: dict, db=None, model=None) -> dict:
    """sources: step 4's section["requests"]; values: step 3's section["values"]; parts: step 5's
    section["requests"]."""
    """db, model: the store and GLiNER2, for stage 2 (without them, stage 1 only)."""
    st, a2 = state(prompt, requests, schema, sources, values, parts), {}
    try:
        answers = await ask(jev, st, questions(requests, schema, sources))
    except Exception as e:                                    # API error: nothing is guessed
        answers = {"error": repr(e)}
    section = build(requests, schema, sources, values, answers)
    if db is not None and model is not None:
        params = {r["id"]: parameters(r["text"], model) for r in requests}
        try:
            a2 = await ask(jev, st, questions_2(db, requests, schema, section["requests"], values, params, parts))
        except Exception as e:
            a2 = {"error": repr(e)}
        build_2(db, requests, schema, section["requests"], values, params, parts, a2, section)
        section["parameters"] = params
    section["jev"] = {"stage_1": answers, "stage_2": a2}
    return section
