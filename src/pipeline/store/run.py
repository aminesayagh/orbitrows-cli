"""The store import from end to end, in two halves so each can be tested alone:

    classify(jev, paths)   the questions, phase by phase over all the files: header, types (with the profile), context,
                           roles, keys, joins, then naming (the user's name for each table split out, or skip: no
                           model call) → {"<file>.csv": its saved sections}
    build(saved, names)    the code: the store schema, with every step's warnings in one list

A model answer below SURE is a question for the user: classify stops after the first phase that has one (Paused); once
the user has answered (client.settle), the import runs again from the start, replaying the recorded answers
(client.Recorded). Every other warning is a notice: shown, and the import goes on. A step's later call can depend on
its earlier answers (the roles decide which columns are linked), so a pause holds the questions of one call per file
and step (per step for keys, one call over all the files): the first call that has any.
"""
import hashlib

from orbitrows.services.csv.reader import read_table
from orbitrows.services.model.client import Model, call_id

from . import context, header, joins, keys, profile, roles, schema, types

NO_CONTEXT = {"context_columns": {}, "source_context": {}}    # the types step runs before the context step
STEPS = ("schema", "context", "roles", "keys", "joins")       # the saved sections that carry "warnings"
PHASES = ("header", "types", "context", "roles", "keys", "joins", "naming")
SECTION = {"types": "schema"}                                 # a phase's warnings are listed under its section, as in build()


class Paused(Exception):
    """The import stopped after `phase`: `questions` for the user (warnings with "question" and a stable "id"), and the
    `notices` of the phases run so far (warnings without a question). Each has "file" and "step"."""

    def __init__(self, phase: str, questions: list, notices: list):
        super().__init__(f"{len(questions)} question(s) after the {phase} step")
        self.phase, self.questions, self.notices = phase, questions, notices


class _Ordered(Model):
    """`inner`, with the call_id of every call in `order`, in the order asked."""

    def __init__(self, inner: Model):
        self.inner, self.name, self.order = inner, inner.name, []

    async def answer(self, state, questions):
        self.order.append(call_id(state, questions))
        return await self.inner.answer(state, questions)


def first_questions(found: list, group, order: list) -> list:
    """The warnings of `found` with a question, each with a stable "id", keeping per group only those of its first
    call (in `order`, _Ordered's) that has any: later calls of the group are asked again after the replay."""
    first = {}                                                # group → its first call with a question
    for w in found:
        if "question" in w:
            g, c = group(w), w["question"]["call"]
            first[g] = min(first.get(g, c), c, key=order.index)
    out = {}                                                  # by id: two warnings may ask the same question
    for w in found:
        if "question" in w and w["question"]["call"] == first[group(w)]:
            i = "q_" + hashlib.sha256((w["question"]["call"] + w["question"]["key"]).encode()).hexdigest()[:8]
            out.setdefault(i, w | {"id": i})
    return list(out.values())


def unnamed(saved: dict, names: dict | None) -> list[dict]:
    """The naming questions: one per table split out of a file (schema.groups) that `names` has no answer for. Its
    answer is any name, or skip for the generated one (the question's "default")."""
    def question(g):
        cols = ", ".join(saved[g["file"]]["read"]["header"][p] for p in [g["key"], *g["columns"]])
        return {"id": g["id"], "file": g["file"], "step": "naming", "question": {"type": "name", "default": g["name"]},
                "problem": f"These columns are their own record: {cols}. Name this table, or skip."}
    return [question(g) for g in schema.groups(saved) if g["id"] not in (names or {})]


async def classify(jev, paths: list, step=lambda text: None, names: dict | None = None) -> dict[str, dict]:
    """{"<file name>": {"read", "schema", "profile", "context", "roles", "keys", "joins"}} for the store files.
    The rows are not kept: read them again with read_table(path, has_header=saved[file]["read"]["has_header"]).
    Raises Paused after the first phase whose warnings carry a question; last, "naming" for every table split out
    of a file that `names` ({question id: name}) has no answer for."""
    saved, tables, notices, jev = {}, {}, [], _Ordered(jev)

    def pause(phase):
        found = [w for f, s in saved.items() for w in _warnings(f, s, SECTION.get(phase, phase))]
        notices.extend(w for w in found if "question" not in w)
        group = (lambda w: w["step"]) if phase == "keys" else (lambda w: (w["file"], w["step"]))
        if questions := first_questions(found, group, jev.order):
            raise Paused(phase, questions, notices)

    for path in paths:
        step(f"{path.name}: header row")
        saved[path.name] = {"read": await header.detect(jev, path)}
    pause("header")
    for path in paths:
        f, s = path.name, saved[path.name]
        tables[f] = table = read_table(path, has_header=s["read"]["has_header"])
        step(f"{f}: column types")
        s["read"] = {"header": table["header"]} | s["read"]
        s["schema"], s["profile"] = await types.classify(jev, table, NO_CONTEXT), profile.profile(table)
    pause("types")
    for f, s in saved.items():
        step(f"{f}: column context")
        s["context"] = await context.classify(jev, tables[f], s["schema"], s["profile"])
    pause("context")
    for f, s in saved.items():
        step(f"{f}: column roles")
        s["roles"] = await roles.classify(jev, tables[f], s["context"], s["schema"], s["profile"])
    pause("roles")
    step("keys between the files")
    found = await keys.classify(jev, [(tables[f], s) for f, s in saved.items()])
    for f, s in saved.items():
        k = found[tables[f]["path"]]
        s["schema"]["create_table"] = k["create_table"]
        s["keys"] = {"warnings": k["warnings"]}
    pause("keys")
    for f, s in saved.items():
        step(f"{f}: joined records")
        s["joins"] = await joins.classify(jev, tables[f], s)
    pause("joins")
    if questions := unnamed(saved, names):
        raise Paused("naming", questions, notices)
    return saved


def _warnings(f: str, s: dict, name: str) -> list[dict]:
    """The warnings of one file's step `name` ("header" or one of STEPS), as build() lists them."""
    if name == "header":
        r = s["read"]
        return [{"file": f, "step": "header", "problem": r["warning"], "probability": r["probability"]}
                | ({"question": r["question"]} if "question" in r else {})] if "warning" in r else []
    out = []
    for w in s.get(name, {}).get("warnings", []):
        if "problem" not in w:                                # the types check: rows that do not fit, listed in w["rows"]
            w = w | {"problem": f'{w["count"]} values do not fit {w["type"]}: '
                                + "; ".join(dict.fromkeys(r["problem"] for r in w["rows"]))}
        out.append({"file": f, "step": name} | w)
    return out


def rows(paths: list, saved: dict) -> dict[str, list]:
    """{"<file name>": its data rows}, read with the header decision of the import."""
    return {p.name: read_table(p, has_header=saved[p.name]["read"]["has_header"])["rows"] for p in paths}


def build(saved: dict, names: dict | None = None) -> dict:
    """{"store": schema.assemble's output, each metadata row with its column's "profile", "warnings": [{"file", "step",
    "column"?, "position"?, "problem", …}]}: every step's warnings, then the schema's problems. A column is named by
    its header ("column") and identified by its position in the file: two columns may share a header. names: the
    user's table names, {naming question id: name}."""
    store = schema.assemble(saved, names)
    for m in store["metadata"]:
        m["profile"] = None                                   # an added id is no column of a file
        if m["position"] is not None:
            p = saved[m["source_file"]]["profile"]
            m["profile"] = p["columns"][m["position"]] | ({"profiled": p["profiled"]} if "profiled" in p else {})
    warnings = [w for f, s in saved.items() for name in ("header", *STEPS) for w in _warnings(f, s, name)]
    file_of = {m["table_id"]: m["source_file"] for m in store["metadata"] if m["source_file"]}
    warnings += [{"file": file_of.get(p["table_id"]), "step": "schema"} | p for p in store["problems"]]
    return {"store": store, "warnings": warnings}
