"""Step 5 of prompt → AST: what each request changes, shows or adds, built as AST expressions (plan S2–S4, decided
2026-10-11). Conditions, sorting and limits are left to the next step (plan S5, S7): this step never reads them.

    Jev   call 1, by statement (step 2), asked per column, like step 4 asks per table (GLiNER2 spans were tried for the
          parts: they miss French, "and" between two changes and "per warehouse"):
            update        per column of the target: does the request change it?
            select        does it show whole records? does it count records? per column of its tables: does it show it?
            insert        per column of the target: does the request give its value?
            alter_table   which action (add · rename · drop a column · rename the table)
            create_table  the table's name; per word of the request: is it one of the new table's columns?
    code  the operands: the numbers of the request (5%, 199), its text values (step 3's values, quoted text, proper
          names, the word after 'to' / 'as' / 'called', the stored values of a column with few of them), the columns
    Jev   call 2, per part found: for a changed column that is not a number, one of the catalog's changes
          (pipeline/operations: set, from another column, prefix, suffix, replace, capitals, lower case, trim, fill the
          empty values, empty it; changes(), 2026-10-25) or none, and its operands (one of the candidates, or none);
          for an output, one of the catalog's outputs (its values, a count, a total, an average…); for a
          schema action, the column, the new name, the type
    code  the value, from the entry's builder (operations/builders.py): the tree is built there, never written by
          Jev. A number column's change, none of the catalog's or one missing a part, is the formula's (formula.py).
          Every request is then compiled (sql.py) and prepared by SQLite (EXPLAIN: no row read): an SQLite error is a
          warning

Section "parts" of cases/<id>.json: {"requests": {"r1": {"assignments": [{"column": "t_x.c_y", "value": expr}],
"outputs": "all" | [{"column": "t_x.c_y" | None, "aggregate": "value" | count …, "expression": expr}],
"insert": {"columns": [...], "values": [expr…]}, "actions": [...], "create": definition, "sql": [...],
"flagged"?}}, "warnings", "jev"}. A column node's source is its table id; the next steps give the aliases.
"""
import re

from text_to_num import alpha2digit

from orbitrows.pipeline.store.profile import listed
from orbitrows.pipeline.operations.builders import BUILDERS
from orbitrows.sql.sql import check, program
from . import SURE
from .asking import question
from orbitrows.services.model.client import ask, choice, yes_no

CONTEXT = ("A merchant wrote a prompt to edit or read their store database (tables and columns in `tables`). It was "
           "split into the requests in `requests`, each with its kind of statement and the table it works on. Each "
           "request now needs what it changes, shows or adds. Its conditions (which records) are handled later.")
CHANGE_RULES = [
    "Answer yes only for a column the request gives a new value to ('lower the price by 5%': the price column).",
    "Answer no for a column the request only uses to choose its records ('the variants priced at 250', 'the black "
    "ones') or to compute the new value ('to the cost plus 10': the cost is not changed).",
]
SHOW_RULES = [
    "A value the request's records do not hold themselves may be stored in the record they belong to: 'the names of "
    "the order lines' are their products' names, 'the city of those orders' is their customer's city: answer yes "
    "for that column.",
    "Answer yes for a column whose values the request shows, sums, averages or compares in its answer ('the names "
    "and prices', 'the total quantity': quantity).",
    "Answer no for a column only used to choose the records ('the draft products': status is not shown) or to sort "
    "them.",
]
GIVE_RULES = ["Answer yes when the prompt gives the value of this column for the new record ('a supplier called "
              "Kora from Peru': the name and the country)."]
FILLED = {"column", "value", "text", "old", "new", "other", "amount"}   # what this step's questions give an entry
_CHANGES = []


def changes(typ: str) -> dict:
    """The catalog's changes (role value, pipeline/operations) this step can build for a column of type `typ`: not a
    number change (the formula's), every required parameter one of FILLED. {entry id: what it does, an example}."""
    if not _CHANGES:
        from orbitrows.pipeline.operations.catalog import connect, entries
        _CHANGES.extend(e for e in entries(connect()) if e["role"] == "value")
    out = {}
    for e in _CHANGES:
        kind = next((p["constraints"].get("data_type") for p in e["parameters"] if p["name"] == "column"), None)
        needs = {p["name"] for p in e["parameters"] if p["required"]}
        if kind in ("numeric", "date") or not needs <= FILLED or (kind == "text" and typ != "TEXT"):
            continue
        out[e["id"]] = f"{e['name']}: {e['behavior']} (e.g. '{e['examples'][0]}')"
    return out


def entry(entry_id):
    return next((e for e in _CHANGES + _OUTPUTS if e["id"] == entry_id), None)


_OUTPUTS = []


def outputs(typ: str) -> dict:
    """What a read can show of a column of type `typ`: the catalog's outputs (role output: its values, a count, a
    total…), a number column's figures (total, average) only for a number. {entry id: what it shows}."""
    if not _OUTPUTS:
        from orbitrows.pipeline.operations.catalog import connect, entries
        _OUTPUTS.extend(e for e in entries(connect()) if e["role"] == "output")
    return {e["id"]: f"{e['name']}: {e['behavior']}" for e in _OUTPUTS
            if typ in ("INTEGER", "REAL") or e["parameters"][0].get("constraints", {}).get("data_type") != "numeric"}


def aggregate_of(e) -> str:
    """The output's aggregate as the next steps name it: value, count, count_distinct, sum, avg, min, max."""
    return e["config"]["function"] + ("_distinct" if e["config"].get("distinct") else "")


ACTIONS = {"add_column": "Add a new column.", "rename_column": "Rename an existing column.",
           "drop_column": "Remove a column.", "rename_table": "Rename the table itself."}
TYPES = {"TEXT": "Text.", "INTEGER": "Whole numbers (quantities, days, counts).",
         "REAL": "Numbers with decimals (prices, weights, percentages)."}
NONE = {"none": "None of these."}
STOP = set("a an the of to in on for and or with by from as at is are be it its their them this that these those "
           "into table column columns called named new each every all please add create make rename remove drop set "
           "change put give type real text integer number nullable".split())


def _schema(schema):
    return {t["definition"]["table_id"]: t["definition"] for t in schema}


def _col(tid, cid):
    return {"type": "column", "source": tid, "reference": cid}


def _lit(v):
    return {"type": "literal", "value": v}


def numbers(text: str) -> list[dict]:
    """The numbers written in a request: [{"id": "n1", "text": "5%", "value": 5.0, "percent": True}]. Number words are
    read too, English and French ("by ten percent", "de quinze pour cent": Codex review, bug 6), by text2num's
    alpha2digit, as the parameters algorithm does; ordinals ("the 2nd", "a third") rank, they are not amounts."""
    words = re.sub(r"\b(?:per\s?cent|pour\s?cent|pourcent)\b", "%", text, flags=re.I)   # before French reads "cent"
    words = alpha2digit(alpha2digit(words, "en", threshold=0), "fr", threshold=0)
    out = []
    for m in re.finditer(r"(?<![\w.])(\d+(?:[.,]\d+)?)(?!st\b|nd\b|rd\b|th\b|e\b|er\b|ème\b)(\s*%)?", words):
        v = float(m.group(1).replace(",", "."))
        out.append({"id": f"n{len(out) + 1}", "text": m.group(0).strip(), "value": int(v) if v.is_integer() else v,
                    "percent": bool(m.group(2))})
    return out


def texts(db, schema, r: dict, values: list[dict], column: tuple | None) -> list[str]:
    """Text values a request may write: step 3's values, quoted text, proper names, the words after to / as /
    called / named, and the stored values of the column when it has few (a status: active, draft, archived)."""
    t, out = r["text"], [v["text"] for v in values if v["request"] == r["id"] and v.get("role") in ("written", "lookup")]
    quoted = [a or b for a, b in re.findall(r"'([^']+)'|\"([^\"]+)\"", t)]     # kept exactly: ' (old)'
    t = re.sub(r"'[^']+'|\"[^\"]+\"", "", t)
    out += re.findall(r"(?<!^)(?<![.!?] )\b([A-Z][\w-]*(?: [A-Z][\w-]*)*)", t)
    out += [m.strip() for m in re.findall(r"\b(?:to|as|called|named|into|by)\s+([^\W\d][\w-]*(?: [^\W\d][\w-]*)?)", t)]
    if column:
        d = _schema(schema)[column[0]]
        name = next(c["sql_name"] for c in d["columns"] if c["column_id"] == column[1])
        stored = listed(db, d["sql_name"], name)              # the profile's values, never the rows
        if len(stored) <= 12:
            out += [s for s in stored if isinstance(s, str)]
    seen = list(quoted)
    for x in out:
        x = x.strip(" ,.;:")
        if x and x.casefold() not in {s.casefold() for s in seen} and x.casefold() not in STOP:
            seen.append(x)
    return seen


def words(text: str) -> list[str]:
    """Names a request may declare (a new column or table): its words and word pairs, snake_case."""
    w = re.findall(r"[^\W\d][\w]*", text.casefold())
    out = [x for x in w if x not in STOP]
    out += [f"{a}_{b}" for a, b in zip(w, w[1:]) if a not in STOP and b not in STOP]
    return list(dict.fromkeys(out))


def main_table(requests: list[dict], sources: dict, rid: str) -> str | None:
    """The stored table a request works on: its target / from, or the table an earlier result came from."""
    for _ in range(len(requests)):
        s = sources.get(rid, {})
        node = s.get("target") or s.get("from") or {}
        if node.get("type") == "database_table":
            return node["reference"]
        if node.get("type") != "request_result":
            return None
        rid = node["request_id"]
    return None


def parents(schema, table) -> list[str]:
    """The tables a record of `table` belongs to, through its foreign keys (a variant → its product → that product's
    supplier and category): a read may show their values ("the names of those" variants: the product's name)."""
    d = {t["definition"]["table_id"]: t["definition"] for t in schema}
    out, todo = [], [table]
    while todo:
        for fk in d.get(todo.pop(), {}).get("foreign_keys", []):
            t = fk["references"]["table"]
            if t not in out and t != table:
                out.append(t)
                todo.append(t)
    return out


def read_tables(schema, sources, rid, main) -> list[str]:
    """A read's tables: its own, step 4's reads, and the tables its records belong to (parents())."""
    return list(dict.fromkeys([main] + sources.get(rid, {}).get("reads", []) + parents(schema, main)))


def shown_columns(schema, tables, main) -> dict:
    """The columns a read may show: its tables' columns but the keys that only link tables (foreign keys, the other
    tables' own ids); the main table's own id stays ("show the variant ids"). Linking keys are how assembly reaches a
    parent's value, not values the merchant asked for ("the names of those": not product_id)."""
    d = {t["definition"]["table_id"]: t["definition"] for t in schema}
    links = {f"{t}.{c}" for t, x in d.items() for fk in x.get("foreign_keys", []) for c in fk["columns"]}
    links |= {f"{t}.{c}" for t, x in d.items() if t != main for c in x.get("primary_key", [])}
    return {k: v for k, v in columns_of(schema, tables).items() if k not in links}


def columns_of(schema, tids) -> dict:
    """{"t_x.c_y": "the y column of x (TYPE)"} for the given tables."""
    d = _schema(schema)
    return {f"{t}.{c['column_id']}": f"The {c['sql_name']} column of {d[t]['sql_name']} ({c['sql_type']})."
            for t in tids if t in d for c in d[t]["columns"]}


def coltype(schema, key: str) -> tuple[str, bool]:
    t, c = key.split(".")
    col = next(x for x in _schema(schema)[t]["columns"] if x["column_id"] == c)
    return col["sql_type"], col["nullable"]


def state(prompt, requests, schema, sources, values) -> dict:
    d = _schema(schema)
    reqs = {}
    for r in requests:
        s = sources.get(r["id"], {})
        main = main_table(requests, sources, r["id"])
        reqs[r["id"]] = {"text": r["text"], "statement": r["statement"], "numbers": [n["text"] for n in numbers(r["text"])],
                         **({"table": d[main]["sql_name"]} if main else {}),
                         **({"also_uses": [d[t]["sql_name"] for t in s.get("reads", []) if t in d]} if s.get("reads") else {})}
    return {"prompt": prompt, "requests": reqs,
            "tables": {x["sql_name"]: [c["sql_name"] for c in x["columns"]] for x in d.values()}}


def said(r):
    return f"request {r['id']} ('{r['text']}')"


def questions_1(requests, schema, sources) -> dict:
    q = {}
    for r in requests:
        rid, st, main = r["id"], r["statement"], main_table(requests, sources, r["id"])
        tables = read_tables(schema, sources, rid, main) if main else []
        if st == "update" and main:
            for key, text in columns_of(schema, [main]).items():
                q[f"{rid}_changes_{key}"] = yes_no(CONTEXT, f"Does {said(r)} give a new value to: {text}", CHANGE_RULES)
        if st == "select" and main:
            q[f"{rid}_whole"] = yes_no(CONTEXT, f"Does {said(r)} show whole records, without naming which of their "
                                                f"values to show ('list the cheap tees': yes; 'show their names': no; "
                                                f"'how many': no)?")
            q[f"{rid}_counts"] = yes_no(CONTEXT, f"Does {said(r)} count the records themselves ('how many sales', "
                                                 f"'the number of products')?")
            for key, text in shown_columns(schema, tables, main).items():
                q[f"{rid}_shows_{key}"] = yes_no(CONTEXT, f"Does {said(r)} show or compute from: {text}", SHOW_RULES)
        if st == "insert" and main:
            for key, text in columns_of(schema, [main]).items():
                q[f"{rid}_gives_{key}"] = yes_no(CONTEXT, f"Does {said(r)} give the value of: {text}", GIVE_RULES)
        if st == "alter_table":
            q[f"{rid}_action"] = choice(CONTEXT, f"What does {said(r)} do to the table?", ACTIONS)
        if st == "create_table":
            names = words(r["text"])
            q[f"{rid}_name"] = choice(CONTEXT, f"What is the name of the table {said(r)} creates?",
                                      {w: f"'{w}'" for w in names} | NONE)
            for w in names:
                q[f"{rid}_has_{w}"] = yes_no(CONTEXT, f"Is '{w}' one of the columns of the new table of {said(r)}? "
                                                      f"(not its name, not a word describing a column)")
    return q


def questions_2(db, requests, schema, sources, values, a1) -> dict:
    q = {}
    for r in requests:
        rid, st, main = r["id"], r["statement"], main_table(requests, sources, r["id"])
        nums = {n["id"]: f"{n['text']}" for n in numbers(r["text"])}
        if st == "update" and main:
            for key in [k.split("_changes_")[1] for k, p in a1.items() if k.startswith(f"{rid}_changes_") and p >= 0.5]:
                typ, _ = coltype(schema, key)
                if typ in ("INTEGER", "REAL"):
                    continue                                    # the formula steps build it (formula.py)
                name = columns_of(schema, [main])[key]
                others = {k: v for k, v in columns_of(schema, [main]).items() if k != key}
                q[f"{rid}_{key}_how"] = choice(CONTEXT, f"How does {said(r)} change {name}",
                                               changes(typ) | {"none": "None of these changes."})
                q[f"{rid}_{key}_number"] = choice(CONTEXT, f"Which number of {said(r)} is the amount, the percentage, "
                                                           f"the new value or the decimals of the change of {name} "
                                                           f"(not a number choosing the records, not a limit)?",
                                                  nums | NONE)
                q[f"{rid}_{key}_column"] = choice(CONTEXT, f"Which other column does the new value of {name} come "
                                                           f"from, in {said(r)}?", others | NONE)
                tx = texts(db, schema, r, values, tuple(key.split(".")))
                q[f"{rid}_{key}_text"] = choice(CONTEXT, f"Which text does {said(r)} write into {name} (the new "
                                                         f"value, the text added, or the replacing text)?",
                                                {f"x{i}": f"'{t}'" for i, t in enumerate(tx)} | NONE)
                q[f"{rid}_{key}_old"] = choice(CONTEXT, f"Which text of {name} does {said(r)} replace, if it "
                                                        f"replaces one?", {f"x{i}": f"'{t}'" for i, t in enumerate(tx)} | NONE)
        if st == "select" and main:
            tables = read_tables(schema, sources, rid, main)
            for key in [k.split("_shows_")[1] for k, p in a1.items() if k.startswith(f"{rid}_shows_") and p >= 0.5]:
                q[f"{rid}_{key}_aggregate"] = choice(CONTEXT, f"What does {said(r)} show of "
                                                              f"{columns_of(schema, tables)[key]}",
                                                     outputs(coltype(schema, key)[0]))
        if st == "insert" and main:
            for key in [k.split("_gives_")[1] for k, p in a1.items() if k.startswith(f"{rid}_gives_") and p >= 0.5]:
                typ, _ = coltype(schema, key)
                name = columns_of(schema, [main])[key]
                opts = nums if typ != "TEXT" else {f"x{i}": f"'{t}'" for i, t in
                                                    enumerate(texts(db, schema, r, values, tuple(key.split("."))))}
                q[f"{rid}_{key}_value"] = choice(CONTEXT, f"Which value does {said(r)} give to {name}", opts | NONE)
        if st == "alter_table" and main:
            action = a1.get(f"{rid}_action", (None,))[0]
            names = {w: f"'{w}'" for w in words(r["text"])} | NONE
            if action in ("add_column", "rename_column", "rename_table"):
                q[f"{rid}_new_name"] = choice(CONTEXT, f"What is the new name in {said(r)}?", names)
            if action == "add_column":
                q[f"{rid}_type"] = choice(CONTEXT, f"What kind of values will the new column of {said(r)} hold?", TYPES)
            if action in ("rename_column", "drop_column"):
                q[f"{rid}_column"] = choice(CONTEXT, f"Which existing column does {said(r)} "
                                                     f"{'rename' if action == 'rename_column' else 'remove'}?",
                                            columns_of(schema, [main]))
        if st == "create_table":
            for w in [k.split("_has_")[1] for k, p in a1.items() if k.startswith(f"{rid}_has_") and p >= 0.5]:
                q[f"{rid}_{w}_type"] = choice(CONTEXT, f"What kind of values will the column '{w}' of the new table "
                                                       f"of {said(r)} hold?", TYPES)
    return q


def build(db, requests, schema, sources, values, a1, a2) -> dict:
    out, warnings = {}, []

    def pick(rid, key, problem, default=None, labels=None):
        """labels: the user's names for opaque options (a number's id, a text's x0), in its question."""
        c, probs = a2.get(key, a1.get(key, (default, {})))
        if c is not None and c != "none" and probs.get(c, 1) < SURE:
            out[rid]["flagged"] = True
            warnings.append({"request": rid, "problem": f"{problem} uncertain", "answer": c,
                             "probability": round(probs.get(c, 0), 2)} | question(a2 if key in a2 else a1, key, labels))
        return None if c == "none" else c

    def unsure_yes(rid, key, what, low=1 - SURE):
        """low: from which probability an unsure answer is flagged (0.5 where only a wrong yes matters)."""
        p = a1.get(key, 0.0)
        if low < p < SURE:
            out[rid]["flagged"] = True
            warnings.append({"request": rid, "problem": f"{what} uncertain", "probability": round(p, 2)}
                            | question(a1, key))
        return p >= 0.5

    for r in requests:
        rid, st, main = r["id"], r["statement"], main_table(requests, sources, r["id"])
        out[rid] = {}
        nums = {n["id"]: n for n in numbers(r["text"])}
        said_nums = {i: n["text"] for i, n in nums.items()}            # a question names a number by its words
        if st in ("update", "select", "insert", "alter_table") and not main:
            warnings.append({"request": rid, "problem": "no stored table: built with the new table's definition, later"})
            continue
        if st == "update":
            out[rid]["assignments"] = []
            for key in columns_of(schema, [main]):
                if not unsure_yes(rid, f"{rid}_changes_{key}", f"changes {key}"):
                    continue
                typ, _ = coltype(schema, key)
                if typ in ("INTEGER", "REAL"):
                    out[rid]["assignments"].append({"column": key, "value": None, "pending": "formula"})
                    continue                                    # its value comes from formula.py
                e = entry(pick(rid, f"{rid}_{key}_how", f"how {key}", labels={i: x["name"] for i, x in
                                                                           ((x["id"], x) for x in _CHANGES)}))
                params = {x["name"] for x in e["parameters"]} if e else set()
                target = _col(*key.split("."))
                p = {"column": target, "column_type": typ}
                if "amount" in params and (num := nums.get(pick(rid, f"{rid}_{key}_number", f"number of {key}",
                                                                labels=said_nums) or "")):
                    p["amount"] = num["value"]
                if "other" in params and (oc := pick(rid, f"{rid}_{key}_column", f"column of {key}")):
                    p["other"] = _col(*oc.split("."))
                if params & {"value", "text", "new", "old"}:
                    tx = texts(db, schema, r, values, tuple(key.split(".")))
                    said_tx = {f"x{i}": t for i, t in enumerate(tx)}
                    if xi := pick(rid, f"{rid}_{key}_text", f"text of {key}", labels=said_tx):
                        p |= {k: _lit(tx[int(xi[1:])]) for k in ("value", "text", "new") if k in params}
                    if "old" in params and (old := pick(rid, f"{rid}_{key}_old", f"replaced text of {key}",
                                                        labels=said_tx)):
                        p["old"] = _lit(tx[int(old[1:])])
                missing = [x["name"] for x in e["parameters"] if x["required"] and x["name"] not in p] if e else ["all"]
                value = None if missing else BUILDERS[e["builder_key"]](p, e["config"])
                if value is None:                               # none of the catalog's, or a part missing ("move the
                    out[rid]["assignments"].append({"column": key, "value": None, "pending": "formula"})
                    continue                                    # date 7 days later"): the formula builds it, or asks
                out[rid]["assignments"].append({"column": key, "value": value, "operation": e["id"]})
            if not out[rid]["assignments"]:
                warnings.append({"request": rid, "problem": "no column changed"})
        if st == "select":
            tables = read_tables(schema, sources, rid, main)
            # a select only reads: a column wrongly left out is seen in the answer, only an unsure yes is flagged
            outs, whole = [], a1.get(f"{rid}_whole", 0.0) >= SURE
            if unsure_yes(rid, f"{rid}_counts", "counts records", 0.5):
                outs.append({"column": None, "aggregate": "count", "expression": {"type": "aggregate", "function": "count"}})
            own_id = {f"{main}.{c}" for x in schema if x["definition"]["table_id"] == main
                      for c in x["definition"].get("primary_key", [])}
            for key in shown_columns(schema, tables, main):
                chooses = any(v["request"] == rid and v.get("role") == "lookup" and v.get("column") == key for v in values)
                # a column whose value chooses the rows ("the Volt variants": brand) is shown only when Jev is sure
                if a1.get(f"{rid}_shows_{key}", 0.0) < (SURE if chooses else 0.5) or (whole and a1.get(f"{rid}_shows_{key}", 0) < SURE):
                    continue
                shown = entry(a2.get(f"{rid}_{key}_aggregate", ("SHOW_VALUES",))[0]) or entry("SHOW_VALUES")
                if outs and key in own_id and aggregate_of(shown).startswith("count"):
                    continue                                  # counting its ids = counting the records: shown once
                unsure_yes(rid, f"{rid}_shows_{key}", f"shows {key}", 0.5)
                shown = entry(pick(rid, f"{rid}_{key}_aggregate", f"aggregate of {key}", "SHOW_VALUES",
                                   labels={x["id"]: x["name"] for x in _OUTPUTS})) or entry("SHOW_VALUES")
                e = BUILDERS[shown["builder_key"]]({"column": _col(*key.split("."))}, shown["config"])
                outs.append({"column": key, "aggregate": aggregate_of(shown), "expression": e,
                             "operation": shown["id"]})
            if not outs:
                whole = unsure_yes(rid, f"{rid}_whole", "whole records", 0.5)
            out[rid]["outputs"] = "all" if whole and not any(o["aggregate"] != "value" for o in outs) else outs
            if not out[rid]["outputs"]:
                warnings.append({"request": rid, "problem": "nothing shown"})
        if st == "insert":
            cols, vals, gives = [], [], []
            for key in columns_of(schema, [main]):
                if not unsure_yes(rid, f"{rid}_gives_{key}", f"gives {key}"):
                    continue
                gives.append(key)                             # an insert from an upload takes these (pipeline.py)
                typ, _ = coltype(schema, key)
                c = pick(rid, f"{rid}_{key}_value", f"value of {key}", labels=said_nums if typ != "TEXT" else {
                    f"x{i}": t for i, t in enumerate(texts(db, schema, r, values, tuple(key.split("."))))})
                if c is None:
                    warnings.append({"request": rid, "problem": f"value of {key} not found"})
                    continue
                if typ == "TEXT":
                    v = texts(db, schema, r, values, tuple(key.split(".")))[int(c[1:])]
                else:
                    v = nums[c]["value"]
                    v = int(v) if typ == "INTEGER" and float(v).is_integer() else v
                cols.append(key.split(".")[1])
                vals.append(_lit(v))
            out[rid]["insert"] = {"columns": cols, "values": vals, "gives": gives}
        if st == "alter_table":
            action = pick(rid, f"{rid}_action", "action")
            new = pick(rid, f"{rid}_new_name", "new name")
            col = pick(rid, f"{rid}_column", "column")
            a = {"add_column": new and {"type": "add_column", "definition": {
                     "column_id": f"c_{new}", "sql_name": new, "header": None,
                     "sql_type": pick(rid, f"{rid}_type", "type") or "TEXT", "nullable": True}},
                 "rename_column": new and col and {"type": "rename_column", "column": col.split(".")[1], "new_name": new},
                 "drop_column": col and {"type": "drop_column", "column": col.split(".")[1]},
                 "rename_table": new and {"type": "rename_table", "new_name": new}}.get(action)
            out[rid]["actions"] = [a] if a else []
            if not a:
                warnings.append({"request": rid, "problem": f"{action}: a part is missing"})
        if st == "create_table":
            name = pick(rid, f"{rid}_name", "table name")
            chosen = [k.split("_has_")[1] for k, p in a1.items() if k.startswith(f"{rid}_has_") and p >= 0.5]
            chosen = [w for w in chosen if w != name and not any(w in o.split("_") and "_" in o for o in chosen)]
            cols = [{"column_id": f"c_{w}", "sql_name": w, "header": None,
                     "sql_type": pick(rid, f"{rid}_{w}_type", f"type of {w}") or "TEXT", "nullable": True}
                    for w in chosen]
            if name and cols:
                out[rid]["create"] = {"table_id": f"t_{name}", "sql_name": name, "columns": cols}
            else:
                warnings.append({"request": rid, "problem": "new table: name or columns missing"})
        # a value pending for the formula steps is checked by assembly, once built
        stmt = statement(r, main, {**out[rid], "assignments": [x for x in out[rid].get("assignments", [])
                                                               if not x.get("pending")]} if out[rid].get("assignments") else out[rid])
        if stmt and not (st == "update" and not stmt.get("assignments")):
            try:
                sql = program([stmt], schema)
                out[rid]["sql"] = sql
                for e in check(db, sql):
                    warnings.append({"request": rid, "problem": "SQLite refuses the statement", "error": e["error"]})
            except Exception as e:                            # a tree the AST refuses: never sent further
                out[rid]["flagged"] = True
                warnings.append({"request": rid, "problem": "AST refused", "error": str(e)[:300]})
    return {"requests": out, "warnings": warnings}


def statement(r, main, part) -> dict | None:
    """The request as one AST statement on all rows (its conditions come later), to compile and check."""
    rid, t = r["id"], {"type": "database_table", "reference": main, "alias": main}
    if part.get("assignments"):
        return {"id": rid, "type": "update", "target": t, "scope": {"type": "all_rows"},
                "assignments": [{"column": _col(*a["column"].split(".")), "value": a["value"]}
                                for a in part["assignments"]]}
    if isinstance(part.get("outputs"), list) and part["outputs"] and all(
            o["column"] is None or o["column"].startswith(f"{main}.") for o in part["outputs"]):
        return {"id": rid, "type": "select", "from": t, "scope": {"type": "all_rows"},
                "columns": [{"expression": o["expression"], "alias": f"o{i}"} for i, o in enumerate(part["outputs"])]}
    if part.get("insert", {}).get("columns"):
        return {"id": rid, "type": "insert", "target": t, "columns": part["insert"]["columns"],
                "values": [part["insert"]["values"]]}
    if part.get("actions"):
        return {"id": rid, "type": "alter_table", "target": {"type": "database_table", "reference": main},
                "actions": part["actions"]}
    if part.get("create"):
        return {"id": rid, "type": "create_table", "definition": part["create"]}
    return None


async def run(jev, db, prompt, requests, schema, sources: dict, values: list[dict]) -> dict:
    """sources: step 4's section["requests"]; values: step 3's section["values"]. A number column's change is left to
    the formula steps (2026-10-15); another column's is one of the catalog's changes (2026-10-25)."""
    a1, a2 = {}, {}
    try:
        st = state(prompt, requests, schema, sources, values)
        a1 = await ask(jev, st, questions_1(requests, schema, sources))
        a2 = await ask(jev, st, questions_2(db, requests, schema, sources, values, a1))
    except Exception as e:                                    # API error: nothing is guessed
        a1 = a1 or {"error": repr(e)}
    section = build(db, requests, schema, sources, values, a1, a2)
    section["jev"] = {"call_1": a1, "call_2": a2}
    return section
