"""The store's links between tables, one resolver for every step (2026-10-26, Codex's review: through(), existence()
and assembly's lift() each walked the foreign keys their own way and could disagree about a link). Code only, from
the schema's foreign keys; a choice between several links is Jev's, asked where it is made (conditions, stage 1).

    edges(schema)                  every foreign key both ways: (from, column, to, column, "up" | "down")
    relationships(schema, table)   the direct links a request can choose by (with / without / how many)
    paths(schema, table, reads)    the links two steps away, through a middle table, to a table the request reads
    path(schema, src, dst)         the shortest chain of keys from src to dst (a condition or a value on another table)
    linked(schema, table)          the chains to every table a record of `table` has one record of
    chained(chain, leaf, ...)      a condition on the chain's last table → on its first: nested IN (SELECT …)
"""
from collections import deque


def defs(schema) -> dict:
    return {x["definition"]["table_id"]: x["definition"] for x in schema}


def edges(schema) -> list[tuple]:
    """Foreign keys both ways: (from table, from column, to table, to column, "up" = child → parent | "down")."""
    out = []
    for t, d in defs(schema).items():
        for fk in d.get("foreign_keys", []):
            (c,), ref = fk["columns"], fk["references"]
            out.append((t, c, ref["table"], ref["columns"][0], "up"))
            out.append((ref["table"], ref["columns"][0], t, c, "down"))
    ups = {e[:4] for e in out if e[4] == "up"}                # a key both ways (an upload's, one-to-one): up each way
    return [e for e in out if e[4] == "up" or e[:4] not in ups]


def relationships(schema, table) -> list[dict]:
    """Every link between `table` and another table by a foreign key, either way (all of them: two keys may reach the
    same table, an order's billing and shipping address), where having a matching record there is a choice the
    request can make: not along a declared, required foreign key to its parent (every variant has its product: the
    schema says so), but along an upload's link (marked virtual: its rows may name records the store doesn't have) or
    an optional one. [{"key": the question's name, "other", "mine": this table's column, "theirs": the other's,
    "children": the other's records point to this one (they can be counted, totalled…)}]."""
    out = []
    for t, x in defs(schema).items():
        for fk in x.get("foreign_keys", []):
            (c,), ref = fk["columns"], fk["references"]
            required = not fk.get("virtual") and not next(
                (col.get("nullable", True) for col in x["columns"] if col["column_id"] == c), True)
            if t == table and ref["table"] != table and not required:
                out.append({"other": ref["table"], "mine": c, "theirs": ref["columns"][0], "children": False})
            elif ref["table"] == table and t != table:
                out.append({"other": t, "mine": ref["columns"][0], "theirs": c, "children": True})
    for r in out:                                             # one link to a table: its name; several: with its key
        twins = sum(1 for x in out if x["other"] == r["other"])
        r["key"] = r["other"] if twins == 1 else f"{r['other']}~{r['theirs'] if r['children'] else r['mine']}"
    return out


def paths(schema, table, reads) -> list[dict]:
    """The links two steps away to a table the request reads (step 4), through a middle table whose records point to
    `table` and whose own linked records point to them (products ← variants ← inventory): each its own question (two
    paths to one table are two choices, never the first taken). [{"key": "final^middle", "other": the final table,
    "mine": `table`'s column, "theirs": the final table's column pointing to the middle, "children": True, "via":
    {"table": the middle, "key": its column pointing to `table`, "id": its column the final table points to}}]."""
    out = []
    for first in relationships(schema, table):
        if not first["children"]:
            continue
        for second in relationships(schema, first["other"]):
            if second["children"] and second["other"] not in (table, first["other"]) and second["other"] in reads:
                out.append({"key": f"{second['other']}^{first['other']}", "other": second["other"],
                            "mine": first["mine"], "theirs": second["theirs"], "children": True,
                            "via": {"table": first["other"], "key": first["theirs"], "id": second["mine"]}})
    return out


def path(schema, src, dst) -> list[tuple] | None:
    """The shortest chain of foreign keys from src to dst, or None."""
    if src == dst:
        return []
    seen, todo, es = {src}, deque([(src, [])]), edges(schema)
    while todo:
        t, p = todo.popleft()
        for e in es:
            if e[0] == t and e[2] not in seen:
                if e[2] == dst:
                    return p + [e]
                seen.add(e[2])
                todo.append((e[2], p + [e]))
    return None


def linked(schema, table) -> dict:
    """{other table: chain of keys [(from table, column, to table, column)]} for each table a record of `table` has
    one record of: many-to-one keys (a variant's product), and a key the other table holds as its own primary key
    (one-to-one: an offer per variant SKU)."""
    d = defs(schema)
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


def chained(chain, leaf, outer_alias, alias_of, sub) -> dict:
    """A condition on the chain's last table → on its first (outer_alias): from the far table back, each level a
    membership col IN (SELECT the next table's key …). leaf(alias of the last table): the condition there.
    alias_of(i, table): level i's alias (taken from the far end, as each caller numbers them); sub(table, alias,
    column, where): the caller's select."""
    aliases = {}
    for i in range(len(chain) - 1, -1, -1):
        aliases[i] = alias_of(i, chain[i][2])
    inner = None
    for i in range(len(chain) - 1, -1, -1):
        t_from, c_from, t_to, c_to = chain[i][:4]
        where = leaf(aliases[i]) if i == len(chain) - 1 else inner
        inner = {"type": "membership", "operator": "in", "query": sub(t_to, aliases[i], c_to, where),
                 "expression": {"type": "column", "source": outer_alias if i == 0 else aliases[i - 1],
                                "reference": c_from}}
    return inner
