"""Step 1 of prompt → AST: the prompt's requests, ordered and linked. The output is the request dependency graph (guide
§1: the third structure, beside the schema and the query AST); the AST statements of later steps carry its ids.

    coref   spaCy mentions + Jev: the words that point to other words ("them", "it", "the copies") (coref.py)
    split   GLiNER2 proposes the cuts, Jev confirms each one; a cut below CUT_MIN keeps the text together (check_cuts.py)
    link    Jev: which request each one waits for, and what it takes from it (link.py)
    code    ids r1…, the execution order, the dependency kinds below

Section "requests" of cases/<id>.json:
    {"requests": [{"id", "text", "start", "end", "marker", "joined_by", "waits_for", "input", "flagged"?}],
     "order": ["r1", …], "links": "r1 THEN r2", "references": [...], "warnings": [...], "jev": {...}}
    input   what a request takes from the request it waits for:
            rows           the rows it returned or changed ("them"): request_result or affected_rows once the
                           operation is known (next step)
            schema_effect  a column or table it creates or renames
            table_state    the table as it leaves it, without pointing back to its rows
            snapshot       waits for nothing: reads the table as it was before the prompt's other requests (AND)
    below 0.8 the likeliest kind is kept and flagged; a link below LINK_MIN is not guessed (link.py: a warning)
"""
import re

from orbitrows.services.model.client import SURE, Answers, ask, choice, yes_no
from . import check_cuts, coref, link
from .asking import question

KIND = {"rows": "rows", "structure": "schema_effect", "table_state": "table_state"}


def store_text(schema: list[dict]) -> str:
    """The store, as the coreference context names it: "their store tables products (product_id, name, …), …"."""
    tables = [f"{t['definition']['sql_name']} ({', '.join(c['sql_name'] for c in t['definition']['columns'])})"
              for t in schema]
    return "their store tables " + "; ".join(tables)


def execution_order(waits_for: list) -> list[int]:
    """Request numbers (1-based) so that each one comes after the one it waits for; otherwise in written order."""
    done = []
    while len(done) < len(waits_for):
        done.append(next(i for i, w in enumerate(waits_for, 1) if i not in done and (w is None or w in done)))
    return done


def build_graph(requests: list[dict], linked: dict, references: list[dict]) -> dict:
    """The section from the split requests, link.link()'s result and the references (coref.by_request)."""
    rid = lambda n: f"r{n}" if n else None
    out, warnings = [], []
    asked = Answers({f"r{i}": (x["choice"], x["probabilities"]) for i, x in enumerate(linked["answers"], 1)}
                    | {f"{k}_input": v for k, v in linked["inputs"].items()})   # link.link's call, for its questions
    asked.call, texts = linked.get("call"), {f"r{i}": r["text"] for i, r in enumerate(requests, 1)}
    for i, (r, w) in enumerate(zip(requests, linked["waits_for"]), 1):
        node = {"id": rid(i), **{k: r[k] for k in ("text", "start", "end", "marker", "joined_by")}, "waits_for": rid(w)}
        if w is None:
            node["input"] = "snapshot"
        else:
            choice, probs = linked["inputs"].get(rid(i), (None, {}))
            node["input"] = KIND.get(choice)
            if probs.get(choice, 0) < SURE:
                node["flagged"] = True
                warnings.append({"request": rid(i), "problem": "input uncertain", "answer": node["input"],
                                 "probability": round(probs.get(choice, 0), 2)} | question(asked, f"{rid(i)}_input"))
        out.append(node)
    for x in linked["warnings"]:                              # request numbers → ids
        warnings.append({k: (rid(v) if k == "request" else [rid(n) for n in v] if k == "requests" else v)
                         for k, v in x.items()} | (question(asked, rid(x["request"]), texts)
                                                   if x["problem"] == "uncertain link" else {}))
    return {"requests": out, "order": [rid(n) for n in execution_order(linked["waits_for"])],
            "links": re.sub(r"\d+", lambda m: f"r{m.group()}", linked["links"]),
            "references": [{k: (rid(v) if k in ("request", "target_request") else v) for k, v in x.items()
                            if k in ("text", "start", "end", "kind", "target", "request", "target_request", "score")}
                           for x in references if x["kind"] != "ambiguous"],
            "warnings": [{"problem": "unresolved reference", "text": x["text"], "request": rid(x["request"]),
                          "probability": x["score"]} | {k: x[k] for k in ("question",) if k in x}
                         for x in references if x["kind"] == "ambiguous"] + warnings}


async def run(jev, prompt: str, mentions: list[dict], model, store: str = coref.STORE) -> dict:
    """The section for one prompt. mentions: coref's for this prompt (coref.mentions runs all prompts at once)."""
    found = await coref.resolve(jev, prompt, mentions, store)
    _, split = await check_cuts.split_checked(jev, prompt, model, found["references"])
    requests = split["requests"]
    references = coref.by_request(found["references"], requests)
    linked = await link.link(jev, prompt, requests, references)
    graph = build_graph(requests, linked, references)
    if split.get("unresolved"):
        graph["warnings"].append({"problem": "split: Jev failed, every cut cancelled"})
    graph["raw_references"] = found["references"]              # with their targets' offsets: for recut()
    ids = {r["id"] for r in graph["requests"]}
    for w in [w for w in graph["warnings"] if w["problem"] == "uncertain link" and w.get("best") in ids
              and w.get("request") in ids and w["best"] != w["request"]]:
        await relate(jev, prompt, graph, w["best"], w["request"])  # "… and their description …": same records?
    graph["jev"] = {"coref": found["answers"], "cuts": [list(c) for c in split.get("cuts", [])],
                    "cut_answers": split.get("answers", []), "link_answers": linked["answers"], "inputs": linked["inputs"]}
    return graph


CUT_CONTEXT = ("A user wrote a prompt to edit or read their store database. One of its requests asks for two "
               "separate actions and must be cut in two: each part one action, with its own words.")
CUT_RULES = [
    "A formula given to some records IN PLACE OF the formula of the others ('…; the Lego sets get 20% off instead', "
    "'…, otherwise add 2', '…; for all the others, take 5 off', '…; the rest get 3') is a branch of the same "
    "change: the cut before it is a continuation (cut apart, those records would get both changes). Two changes "
    "of different records that do not replace each other ('add 3 to the Lego stock; the Duplo stock gets 2') may "
    "be two requests.",
    "Choose the cut where the second action's words start, keeping each action with its object and conditions: "
    "'Before you list their sizes, | raise the price of the red caps' ; 'Show the stock of the white mugs | once you "
    "have archived them'.",
    "A part that points to the other one ('their', 'them', 'those') still stands alone: that link is resolved after "
    "the cut. Choose no cut only when one part has no action of its own.",
]
ROWS_RULES = [
    "Answer yes when this part shows, counts or changes the records the other part changes or reads: 'show those', "
    "'list their sizes', 'show the new prices' after a change of those prices.",
    "Answer no when this part works on records of its own, named in its own words.",
]
DESCRIBE_RULES = [
    "Answer yes when words of this part ('them', 'their', 'those') mean the records the other part describes in its "
    "own words: in 'Show the stock of the white mugs | once you have archived them', 'them' are the white mugs.",
    "Answer no when this part names its own records ('raise the price of the red caps'), or when its words point to "
    "nothing in the other part.",
]


def cut_points(prompt: str, req: dict, model) -> list[int]:
    """Where a request may be cut: after its punctuation (, ; :) and before GLiNER2's actions, joiners and markers."""
    a, b = req["start"], req["end"]
    out = {m.end() for m in re.finditer(r"[,;:]\s*", prompt[a:b])}
    out = {a + x for x in out}
    spans = split_module().find_spans(prompt[a:b], model)
    for label in ("action", "joiner", "marker"):
        out |= {a + s["start"] for s in spans.get(label, [])}
    acts = sorted(spans.get("action", []), key=lambda s: s["start"])
    for x, y in zip(acts, acts[1:]):                  # between two actions: every word may start the second one
        out |= {a + m.start() for m in re.finditer(r"(?<=\s)\S", prompt[a:b]) if x["end"] < m.start() <= y["start"]}
    return sorted(x for x in out if a < x < b and prompt[a:x].strip(" ,;:") and prompt[x:b].strip(" ,;:."))


def split_module():
    from . import split
    return split


async def relate(jev, prompt: str, graph: dict, first: str, other: str) -> dict:
    """Two requests whose link is not sure (a request cut in two, or step 1's uncertain link): does `other` work on
    the records `first` changes or reads ("show those", "their description": it takes those rows, after `first`)?
    do words of `first` ("them") mean the records `other` describes (it inherits `other`'s values, pipeline)?
    Changes `graph` in place; returns Jev's answers."""
    texts = {r["id"]: r["text"] for r in graph["requests"]}
    ans = await ask(jev, {"prompt": prompt, "parts": {first: texts[first], other: texts[other]}},
                    {"describes": yes_no(CUT_CONTEXT, f"In part {first} ('{texts[first]}'), do words like 'them' or "
                                                      f"'those' mean the records part {other} ('{texts[other]}') "
                                                      f"describes?", DESCRIBE_RULES),
                     "rows": yes_no(CUT_CONTEXT, f"Does part {other} ('{texts[other]}') work on the records part "
                                                 f"{first} ('{texts[first]}') changes or reads?", ROWS_RULES)})
    rows = ans.get("rows", 0.0)
    if rows >= 0.5:                                           # "show those": the later part takes the first's rows
        for r in graph["requests"]:
            if r["id"] == other:
                r.update(waits_for=first, input="rows")
                r.pop("flagged", None)
        graph["warnings"] = [w for w in graph["warnings"] if not (w.get("request") == other and
                                                                  w["problem"] in ("input uncertain", "uncertain link"))]
        graph["order"] = [x for x in graph["order"] if x != other]
        graph["order"].insert(graph["order"].index(first) + 1, other)
    if 1 - SURE < rows < SURE:
        graph["warnings"].append({"request": other, "problem": "whether it works on the rows of the other part: "
                                                               "uncertain", "probability": round(rows, 2)}
                                 | question(ans, "rows"))
    p = ans.get("describes", 0.0)
    if p >= 0.5:
        graph.setdefault("inherits", {})[first] = other
    if 1 - SURE < p < SURE:
        graph["warnings"].append({"request": first, "problem": "what 'them' / 'those' refers to: uncertain",
                                  "probability": round(p, 2)} | question(ans, "describes"))
    return ans


async def recut(jev, prompt: str, graph: dict, rid: str, model) -> dict | None:
    """Step 1 again for one request step 2 found two actions in: Jev chooses where the second starts among
    cut_points(), then the links and references are computed for the new requests. None when no cut is sure."""
    reqs = [dict(r) for r in graph["requests"]]
    req = next(r for r in reqs if r["id"] == rid)
    points = cut_points(prompt, req, model)
    if not points:
        return None
    opts = {f"c{i}": f"'{prompt[req['start']:x].strip(' ,;:')}' | '{prompt[x:req['end']].strip()}'"
            for i, x in enumerate(points)} | {"none": "No cut: the parts cannot stand alone."}
    ans = await ask(jev, {"prompt": prompt, "request": req["text"]},
                    {"cut": choice(CUT_CONTEXT, f"Where does the second action of '{req['text']}' start?", opts, CUT_RULES)})
    pick, probs = ans.get("cut", (None, {}))
    # step 2 found two actions: not cutting is surely wrong (one action would be lost), so the likeliest cut is
    # taken from 0.5, flagged below SURE
    if pick in (None, "none") or probs.get(pick, 0) < 0.5:
        return None
    x = points[int(pick[1:])]
    left_end = req["start"] + len(prompt[req["start"]:x].rstrip(" ,;:"))
    right_start = x + len(prompt[x:req["end"]]) - len(prompt[x:req["end"]].lstrip())
    parts = [{"text": prompt[req["start"]:left_end], "start": req["start"], "end": left_end, "marker": req.get("marker"),
              "joined_by": req.get("joined_by")},
             {"text": prompt[right_start:req["end"]], "start": right_start, "end": req["end"], "marker": None,
              "joined_by": None}]
    i = reqs.index(req)
    split_reqs = [{k: r[k] for k in ("text", "start", "end", "marker", "joined_by")} for r in reqs[:i]] + parts + \
                 [{k: r[k] for k in ("text", "start", "end", "marker", "joined_by")} for r in reqs[i + 1:]]
    references = coref.by_request(graph.get("raw_references", []), split_reqs)
    linked = await link.link(jev, prompt, split_reqs, references)
    out = build_graph(split_reqs, linked, references)
    out["raw_references"] = graph.get("raw_references", [])
    out["recut"] = {"request": rid, "at": x, "probability": round(probs.get(pick, 0), 2), "options": opts}
    if probs.get(pick, 0) < SURE:
        out["warnings"].append({"request": rid, "problem": "where the request was cut in two: uncertain",
                                "probability": round(probs.get(pick, 0), 2)} | question(ans, "cut", opts))
    new_ids = [f"r{i + 1}", f"r{i + 2}"]                      # the two parts of the cut request
    waiting = [r for r in out["requests"] if r["id"] in new_ids and r["waits_for"] in new_ids]
    first, other = (waiting[0]["waits_for"], waiting[0]["id"]) if waiting else (new_ids[0], new_ids[1])
    desc = await relate(jev, prompt, out, first, other)
    out["jev"] = {**graph.get("jev", {}), "recut": ans, "describes": desc, "link_answers": linked["answers"]}
    return out
