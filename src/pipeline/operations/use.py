"""Using the catalog the same way in every step (2026-10-28, phase 1 of "the operations served end to end"): a step
that needs an operation searches the catalog, shows Jev the candidates (selector.py), and fills the chosen entry from
its own declaration; no step keeps its own list of operations or its own rules for an entry's parameters.

    search(text, role, kind=None, k=K)   the k enabled entries of `role` closest to `text` (index.py), only those that
                                         apply to a column of `kind` (number · text · date; None: any) and that a
                                         request can fill (an entry built from what a step computes itself, the
                                         linked records' comparison, is stage 2c's)
    slots(entry)                         what a request fills: the entry's declared parameters but the column it applies
                                         to and the ones the code sets (a column's own type)
    fill(entry, given)                   the entry's parameters from the step's values ({slot: value}, the column as
                                         "column"), checked against the declaration: → (parameters, problems); a
                                         required slot missing, a value outside its declared choices, a column of
                                         the wrong type is a problem, never a guess; a missing optional slot keeps
                                         its declared default
    build(entry, parameters)             the entry's own builder: our AST (sql/dml.py)
    reads_column(entry)                  whether its value depends on the value it changes (probed, not listed)

Values: a number or a percentage as a number, or a node (another step's result: "never above twice the cost"); a
text or a value as a text (it becomes a literal) or a node; a column as a column node, but the value an entry applies
to ("column") may be another step's result ("round the discounted price"); an operator, a unit or an aggregate as one
of the declared choices.
"""
from .builders import BUILDERS

K = 10                                                        # the candidates a step shows Jev
KINDS = {"number": ("numeric",), "text": ("text",), "date": ("date",)}   # a column's kind → the entries' declared type
SET_BY_CODE = {"sql_type", "row_key"}                                    # parameter types a step sets itself, never the request
_CATALOG = {}


def catalog() -> dict:
    """{entry id: entry} of the enabled entries, and the search over them, loaded once per process."""
    if not _CATALOG:
        from .catalog import connect, entries
        from .index import Index
        db = connect()
        _CATALOG.update(entries={e["id"]: e for e in entries(db)}, index=Index(db))
    return _CATALOG


def applies(entry: dict, kind: str | None) -> bool:
    """The entry works on a column of this kind: its column parameter's declared type, or none declared (any)."""
    if kind is None:
        return True
    col = next((p for p in entry["parameters"] if p["name"] == "column"), None)
    declared = (col or {}).get("constraints", {}).get("data_type")
    return declared is None or declared in KINDS.get(kind, ())


def search(text: str, role: str, kind: str | None = None, k: int = K) -> list[str]:
    c = catalog()
    ranked = [i for i, _ in c["index"].retrieve(text, len(c["entries"]))]
    return [i for i in ranked if c["entries"][i]["role"] == role and applies(c["entries"][i], kind)
            and c["entries"][i]["builder_key"] != "linked_aggregate"][:k]


def slots(entry: dict) -> list[dict]:
    return [p for p in entry["parameters"] if p["name"] != "column" and p["type"] not in SET_BY_CODE]


def fill(entry: dict, given: dict) -> tuple[dict, list[str]]:
    params, problems = {}, []
    for p in entry["parameters"]:
        name, v = p["name"], given.get(p["name"])
        if v is None:
            if "default" in p:
                params[name] = p["default"]
            elif p["required"] and p["type"] not in SET_BY_CODE:
                problems.append(f"{entry['id']}: {name} missing ({p.get('description', '')})")
            continue
        choices = p.get("constraints", {}).get("one_of")
        if choices and not isinstance(v, dict) and v not in choices:
            problems.append(f"{entry['id']}: {name} is {v!r}, not one of {choices}")
            continue
        if p["type"] in ("number", "percentage") and not isinstance(v, (int, float, dict)):
            problems.append(f"{entry['id']}: {name} is {v!r}, not a number")
            continue
        if p["type"] == "column_reference" and not (isinstance(v, dict) and (v.get("type") == "column" or (
                p["name"] == "column" and "type" in v))):     # what an entry applies to: a column, or a step's result
            problems.append(f"{entry['id']}: {name} is {v!r}, not a column")
            continue
        params[name] = {"type": "literal", "value": v} if p["type"] in ("text", "value") and not isinstance(v, dict) \
            else v
    return params, problems


def reads_column(entry: dict) -> bool:
    """Whether the entry's value depends on the value it changes (raise it, round it, prefix it) or not (set it to a
    value, copy another column, empty it): its builder probed with a marked column, every slot a sample value."""
    import json
    sample = {"number": 1, "percentage": 1, "text": "x", "value": "x", "condition": None, "sql_type": "TEXT",
              "column_reference": {"type": "column", "source": "t_", "reference": "c_other"}}
    given = {"column": {"type": "column", "source": "t_", "reference": "c_probe"}}
    for p in slots(entry):
        given[p["name"]] = (p.get("constraints", {}).get("one_of") or [sample.get(p["type"], 1)])[0]
    params, problems = fill(entry, given)
    return bool(problems) or "c_probe" in json.dumps(build(entry, params))


def build(entry: dict, params: dict) -> dict:
    return BUILDERS[entry["builder_key"]](params, entry["config"])
