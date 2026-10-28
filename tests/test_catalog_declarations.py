"""The catalog's entries declare what their builders read (no model loaded): a step that fills an entry from its
declaration alone (its parameters, their types and descriptions) gets every value the builder needs, and nothing it
ignores. Read from the builders' code: p["x"] is a value the builder needs, p.get("x") one it may take."""
import ast
import inspect

from orbitrows.pipeline.operations import builders
from orbitrows.pipeline.operations.catalog import connect, entries

TYPES = {"column_reference", "number", "percentage", "text", "value", "comparison_operator", "aggregate",
         "linked_table", "date_unit", "condition", "sql_type", "order_direction"}
MIN_EXAMPLES = 3


def reads(fn) -> tuple[set, set]:
    """(the keys a builder needs: p["x"], the keys it may take: p.get("x"))."""
    need, may = set(), set()
    for n in ast.walk(ast.parse(inspect.getsource(fn))):
        if isinstance(n, ast.Subscript) and getattr(n.value, "id", None) == "p" and isinstance(n.slice, ast.Constant):
            need.add(n.slice.value)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and getattr(n.func.value, "id", None) == "p" \
                and n.func.attr == "get" and n.args and isinstance(n.args[0], ast.Constant):
            may.add(n.args[0].value)
    return need, may


def test_every_entry_declares_what_its_builder_reads():
    es = entries(connect())
    by_builder = {}
    for e in es:
        by_builder.setdefault(e["builder_key"], set()).update(p["name"] for p in e["parameters"])
    wrong = []
    for e in es:
        declared = {p["name"] for p in e["parameters"]}
        need, may = reads(builders.BUILDERS[e["builder_key"]])
        wrong += [f"{e['id']}: needs {k}, not declared" for k in need - declared]
        wrong += [f"{e['id']}: may take {k}, declared by no entry of its builder" for k in may - by_builder[
            e["builder_key"]]]
        wrong += [f"{e['id']}: declares {k}, its builder never reads it" for k in declared - need - may - {"column"}]
        wrong += [f"{e['id']}: {p['name']} of unknown type {p['type']}" for p in e["parameters"] if p["type"] not in TYPES]
        wrong += [f"{e['id']}: {p['name']} has no description" for p in e["parameters"] if not p.get("description")]
        wrong += [f"{e['id']}: no column parameter"] if "column" not in declared else []
    assert not wrong, wrong


def test_every_entry_has_its_own_examples():
    es = entries(connect())
    seen, wrong = {}, []
    for e in es:
        wrong += [f"{e['id']}: {len(e['examples'])} examples"] if len(e["examples"]) < MIN_EXAMPLES else []
        wrong += [f"{e['id']}: an example repeated"] if len(set(e["examples"])) != len(e["examples"]) else []
        for x in e["examples"]:
            if x.casefold() in seen:
                wrong.append(f"{e['id']} and {seen[x.casefold()]} share the example {x!r}")
            seen[x.casefold()] = e["id"]
    assert not wrong, wrong
