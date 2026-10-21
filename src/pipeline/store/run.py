"""The store import from end to end, in two halves so each can be tested alone:

    classify(jev, paths)   the questions: header, types, profile, context, roles per file, then keys over all the files,
                           then joins per file → {"<file>.csv": its saved sections}
    build(saved, rows)     the code: the store schema and the round-trip proof, with every step's warnings in one list
"""
from orbitrows.services.csv.reader import read_table

from . import context, header, joins, keys, profile, roles, schema, types

NO_CONTEXT = {"context_columns": {}, "source_context": {}}    # the types step runs before the context step
STEPS = ("schema", "context", "roles", "keys", "joins")       # the saved sections that carry "warnings"


async def classify(jev, paths: list, step=lambda text: None) -> dict[str, dict]:
    """{"<file name>": {"read", "schema", "profile", "context", "roles", "keys", "joins"}} for the store files.
    The rows are not kept: read them again with read_table(path, has_header=saved[file]["read"]["has_header"])."""
    saved, tables = {}, {}
    for path in paths:
        f = path.name
        step(f"{f}: header row")
        read = await header.detect(jev, path)
        table = read_table(path, has_header=read["has_header"])
        step(f"{f}: column types")
        s = {"read": {"header": table["header"]} | read, "schema": await types.classify(jev, table, NO_CONTEXT),
             "profile": profile.profile(table)}
        step(f"{f}: column context")
        s["context"] = await context.classify(jev, table, s["schema"], s["profile"])
        step(f"{f}: column roles")
        s["roles"] = await roles.classify(jev, table, s["context"], s["schema"], s["profile"])
        saved[f], tables[f] = s, table
    step("keys between the files")
    found = await keys.classify(jev, [(tables[f], s) for f, s in saved.items()])
    for f, s in saved.items():
        k = found[tables[f]["path"]]
        s["schema"]["create_table"] = k["create_table"]
        s["keys"] = {"warnings": k["warnings"]}
    for f, s in saved.items():
        step(f"{f}: joined records")
        s["joins"] = await joins.classify(jev, tables[f], s)
    return saved


def rows(paths: list, saved: dict) -> dict[str, list]:
    """{"<file name>": its data rows}, read with the header decision of the import."""
    return {p.name: read_table(p, has_header=saved[p.name]["read"]["has_header"])["rows"] for p in paths}


def build(saved: dict, rows: dict) -> dict:
    """{"store": schema.assemble's output, "diffs": schema.round_trip's, "warnings": [{"file", "step", "column"?,
    "problem", …}]}: every step's warnings, then the schema's problems."""
    store = schema.assemble(saved)
    warnings = []
    for f, s in saved.items():
        if w := s["read"].get("warning"):
            warnings.append({"file": f, "step": "header", "problem": w, "probability": s["read"]["probability"]})
        for name in STEPS:
            for w in s.get(name, {}).get("warnings", []):
                if "problem" not in w:                        # the types check: rows that do not fit, listed in w["rows"]
                    w = w | {"problem": f'{w["count"]} values do not fit {w["type"]}: '
                                        + "; ".join(dict.fromkeys(r["problem"] for r in w["rows"]))}
                warnings.append({"file": f, "step": name} | w)
    file_of = {m["table_id"]: m["source_file"] for m in store["metadata"] if m["source_file"]}
    warnings += [{"file": file_of.get(p["table_id"]), "step": "schema"} | p for p in store["problems"]]
    return {"store": store, "diffs": schema.round_trip(store, rows), "warnings": warnings}
