"""The proof of concept's saved sections (fixtures/cases/*/result.json) as the import steps give them now: the PoC keyed
its columns by header, the steps key them by id, the column's position in its file. The saved files repeat no header."""
import json
from pathlib import Path

CASES = Path(__file__).parent / "fixtures" / "cases"


def tables(case: str) -> dict:
    """{"<file>.csv": its saved sections}, columns by position."""
    return {f: by_position(s) for f, s in json.loads((CASES / case / "result.json").read_text())["tables"].items()}


def by_position(s: dict) -> dict:
    """One file's saved sections, its columns by position; every warning about a column gets its "position"."""
    header = s["read"]["header"]
    assert len(set(header)) == len(header), "a repeated header: the PoC's keys cannot name its columns"
    p = header.index
    s = json.loads(json.dumps(s))
    s["profile"]["columns"] = {p(h): c | ({"determines": [p(d) for d in c["determines"]]} if "determines" in c else {})
                               for h, c in s["profile"]["columns"].items()}
    ctx = s["context"]
    ctx["context_columns"] = {p(h): e | {"describes": [p(d) for d in e["describes"]]}
                              for h, e in ctx["context_columns"].items()}
    ctx["source_context"] = {p(h): e | ({"column": p(e["column"])} if e["from"] == "column" else {})
                             for h, e in ctx["source_context"].items()}
    s["roles"]["columns"] = {p(h): r for h, r in s["roles"]["columns"].items()}
    if "joins" in s:
        j = s["joins"]
        j["groups"] = [{"key": p(g["key"]), "columns": [p(c) for c in g["columns"]]} for g in j["groups"]]
        if "remaining" in j:
            j["remaining"] = [p(c) for c in j["remaining"]]
    for section in s.values():
        for w in section.get("warnings", []) if isinstance(section, dict) else []:
            if w.get("column") is not None:
                w["position"] = p(w["column"])
    return s
