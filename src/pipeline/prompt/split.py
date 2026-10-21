"""
Split a user prompt into separate requests with GLiNER2 (moved from prompt_edit/split_requests.py, 2026-10-10;
logic unchanged except, 2026-10-10: a word also found as an action is never a joiner (plan_cuts); a list joiner
may have up to two words, none of them an action, between it and a scope (_list_gap). Terms follow docs/request-definition.md: request = action + target + scope + parameters + constraints.

    .venv/bin/python -m v2.prompt_ast.split "lower the price by 10% for Samsung products, then set the stock to 50"

prints, in prompt order:

    ["lower the price by 10% for Samsung products", "set the stock to 50"]

GLiNER2 spans are at most 8 word tokens (max_width 8), too short for a whole request.
So GLiNER2 finds three kinds of short phrases, and the prompt is cut between them:

    action  where a request starts:          "raise the prices", "set the stock"
    scope      which rows it applies to:        "for Samsung and Bose products", "over 1,299 dollars"
    joiner     a word joining two requests:     "and", "then", "et", "puis", "und", "luego"

The words in JOINERS are only examples written into GLiNER2's label description: GLiNER2 decides, in
context, which "and" joins two requests. Code never searches the prompt for words; it only looks for
punctuation, which is the same in every language.

Rules:
    1. Between two actions, cut at the strongest separator outside any scope: end of sentence
       (. ; ! ? new line), then a joiner, then a comma not followed by a digit. Text before the cut
       stays with the earlier request, text after it with the later one; text before the first
       action stays with the first request.
    2. A joiner between two scopes ("for Samsung and for Bose products") joins a list, not two requests.
       When every separator between two actions joins scopes, the cut is still proposed, with a note for Jev;
       so is a cut next to an action that starts inside a scope. The splitter proposes; check_cuts.py (Jev) decides.
    3. Punctuation and joiners left at the edges of a request are trimmed.
"""

import json
import re
import sys

from orbitrows.services.extractor import client as extractor

# Example words shown to GLiNER2, by language. A new language only needs its words here.
# Keep the non-English examples even for English prompts: English-only examples made English worse
# (blocking errors on 116 English cases: 5 with en/fr/de/es, 14 with English only). Adding it/pt: no gain.
JOINERS = {
    "en": ["and", "then", "and then", "also", "after that"],
    "fr": ["et", "puis", "ensuite", "et puis", "aussi"],
    "de": ["und", "dann", "danach", "außerdem"],
    "es": ["y", "luego", "después", "también"],
}


# Sequence markers (option C, 2026-10-01): removed from the request text but kept as its "marker", for a later step
# that orders requests or decides how they are linked. Example words shown to GLiNER2, by language.
MARKERS = {
    "en": ["first", "firstly", "second", "secondly", "third", "next", "finally", "lastly"],
    "fr": ["d'abord", "premièrement", "deuxièmement", "enfin", "pour finir"],
    "de": ["zuerst", "erstens", "zweitens", "schließlich", "zum Schluss"],
    "es": ["primero", "segundo", "por último", "finalmente"],
}


def _examples(words):
    return ", ".join(f"'{w}'" for ws in words.values() for w in ws)


# Thresholds: lower = accepts lower-scoring spans; they don't control span length.
# action 0.2 was best on the first 23 test cases (21/23 vs 16/23 at 0.4). The others aren't tuned yet.
# action 0.25: a request split in two blocks the next step, two requests merged don't (decided 2026-10-01).
# On 175 cases: blocking errors 9 at 0.2, 6 at 0.25, 6 at 0.3-0.35, 4 at 0.5; merged 8, 18, 23-27, 61.
LABELS = {
    # "action", not "operation": the label name is part of GLiNER2's input, and "operation" missed
    # the French "mets" that "action" finds (0.21-0.23).
    # Generic description (2026-10-01): GLiNER2 only proposes cuts, Jev decides, so finding every action matters
    # more than avoiding false ones. With the Jev check (CUT_MIN 0.9) on 182 cases: right 142 against 135, merged 39
    # against 47; the false actions it adds (prepositions, constraints) were all cancelled by Jev.
    "action": {
        "description": "A short phrase expressing a requested operation on table data or structure, including "
                       "retrieving, counting, comparing, modifying, adding, or deleting.",
        "threshold": 0.25,
    },
    "scope": {
        "description": "which products the request applies to: a brand, a product, or a condition on a column, "
                       "like 'for Bose products' or 'where the stock is under 5'",
        "threshold": 0.3,
    },
    "joiner": {
        "description": f"a word joining two separate requests, like {_examples(JOINERS)}. "
                       "Not a word joining two products of the same request",
        "threshold": 0.3,
    },
    "marker": {
        "description": f"a word giving the position of a step in a sequence, like {_examples(MARKERS)}",
        "threshold": 0.3,
    },
}

# Notes passed to Jev with a cut: evidence, not decisions. The splitter proposes; Jev decides.
NOTE_LIST = "every separator here sits between two parts of a list of products or conditions"
NOTE_IN_SCOPE = "an action next to this cut starts inside a scope, so it may be a verb of that scope's condition"

STOP = r"[.;!?](?=\s|$)|\n"          # "." must be followed by a space or the end, so 12.5 stays whole
COMMA = r",(?!\d)"                   # a comma followed by a digit is part of a number: 1,299 or 21,90
# Trimmed at request edges. A cut already takes its own separator (".", ",", "and"), so next to a cut only layout and
# a leftover "," or ";" are trimmed ("…by 10%, and set…"); at the prompt's own start and end, only layout. Other
# punctuation can be a value and is kept ("set the tag to ?"). Bullets at the start of a request: LIST_START.
SPACE = " \t\r\n"
TRIM = SPACE + ",;"
# A list marker at the start ("1. ", "2) ", ") ", "- ") or the next item's number left at the end ("…to 4\n3")
LIST_START = re.compile(r"\s*(?:\d*[.)]|[-*•])\s+")
LIST_END = re.compile(r"\s*\n\s*\d+[.)]?\s*$")

def find_spans(prompt, model=None):
    """GLiNER2 phrases by label, each list non-overlapping and in prompt order."""
    # One call per label: when all label descriptions together are too long, GLiNER2 silently returns
    # nothing (it did with the full dictionary). Separate calls also keep each label's scores independent.
    model = model or extractor.connect()
    spans = {}
    for label, config in LABELS.items():
        result = model.extract_entities(
            prompt, {label: config}, include_confidence=True, include_spans=True, overlap_policy="flat",
        ).get("entities", {})
        spans[label] = _flat(result.get(label, []))
    return spans


def _flat(spans):
    kept = []
    for s in sorted(spans, key=lambda s: -s.get("confidence", 0.0)):
        if all(s["end"] <= k["start"] or s["start"] >= k["end"] for k in kept):
            kept.append(s)
    return sorted(kept, key=lambda s: s["start"])


def _inside(pos, spans):
    return any(s["start"] <= pos < s["end"] for s in spans)


def _list_gap(prompt, a, b, actions):
    """prompt[a:b] sits between a list joiner and its scope: punctuation and at most two words without digits, none of
    them part of an action. GLiNER2 returns scopes without their preposition ("Samsung" for "for Samsung", "Casa
    variants" for "for the Casa variants"), so "and for the Bose products" must still join a list (2026-10-10). Counts
    words and checks GLiNER2's actions, never reads the words: the same in every language."""
    return (re.fullmatch(r"[\s,;:]*(?:[^\W\d_]+[\s,;:]*){0,2}", prompt[a:b]) is not None
            and not any(x["start"] < b and a < x["end"] for x in actions))


def _choose_cut(prompt, g0, g1, scopes, joiners, actions=()):
    """Where to cut the gap prompt[g0:g1] between two actions: (start, end, note or None)."""
    gap_scopes = [s for s in scopes if g0 <= s["start"] and s["end"] <= g1]
    classes = [
        [(g0 + m.start(), g0 + m.end()) for m in re.finditer(STOP, prompt[g0:g1])],
        [(j["start"], j["end"]) for j in joiners if g0 <= j["start"] and j["end"] <= g1],
        [(g0 + m.start(), g0 + m.end()) for m in re.finditer(COMMA, prompt[g0:g1])],
    ]
    unsuitable = False                               # separators found, but each one joins two scopes
    for strength, separators in enumerate(classes):
        found = []                                   # (start, end, joins two scopes)
        for pos, end in separators:
            if _inside(pos, scopes):
                continue
            before = [s for s in gap_scopes if s["end"] <= pos]
            after = [s for s in gap_scopes if s["start"] >= end]
            joins = bool(strength and before and after          # rule 2
                         and _list_gap(prompt, before[-1]["end"], pos, actions)
                         and _list_gap(prompt, end, after[0]["start"], actions))
            found.append((pos, end, joins))
        free = [c for c in found if not c[2]]
        if free:
            return (*free[-1][:2], None)             # cut at the last acceptable one
        if found and not unsuitable:
            unsuitable = found[-1][:2]               # only list joiners here: try weaker separators first
    # Every separator joins two scopes ("for Samsung phones and Bose speakers set …"): still propose the last one,
    # with a note, and let Jev decide.
    if unsuitable:
        return (*unsuitable, NOTE_LIST)
    return g1, g1, None                              # no separator at all: cut right before the next action


def _bounds(prompt, a, b, edges, cut_before=True, cut_after=True):
    """Trim prompt[a:b]: punctuation, list markers and GLiNER2 joiners / markers at its edges ("…by 5% and",
    "Also clear…", "2. set…", "Second, set…"). Returns (start, end, marker): the marker removed from its start.
    Joiners are only removed next to a cut (cut_before / cut_after): elsewhere a "joiner" is part of the text
    ("rename the product to Now and Then")."""
    marker = None
    head_trim, tail_trim = (TRIM if cut_before else SPACE), (TRIM if cut_after else SPACE)
    while True:
        while a < b and prompt[a] in head_trim:
            a += 1
        while b > a and prompt[b - 1] in tail_trim:
            b -= 1
        m = LIST_START.match(prompt, a, b)
        if m and m.end() > a:                                    # "1. ", "2) ", "- "
            if re.search(r"\d", m.group()) and marker is None:
                marker = m.group().strip()
            a = m.end()
            head_trim = TRIM                                     # punctuation after a marker belongs to it
            continue
        m = LIST_END.search(prompt, a, b)                        # the next item's number: "… to 4\n3"
        if m and prompt[b:b + 1] in (".", ")"):                  # "2024" at the end of a line is a value
            b = m.start()
            continue
        head = next((e for e in edges if e["start"] == a and e["end"] <= b
                     and (cut_before or e.get("kind") == "marker")), None)
        if head:
            if head.get("kind") == "marker" and marker is None:
                marker = head["text"]
            a = head["end"]
            head_trim = TRIM                                     # "First, delete…": the comma goes with "First"
            continue
        tail = next((e for e in edges if e["end"] == b and e["start"] >= a and e.get("kind") != "marker"
                     and cut_after), None)
        if tail:
            b = tail["start"]
            continue
        return a, b, marker


def _whole_word(prompt, span):
    """The span grown to its whole word: GLiNER2 returns "abord" for "D'abord"."""
    start, end = span["start"], span["end"]
    word = lambda ch: ch.isalnum() or ch in "'’-"           # D'abord, schließlich: letters, digits, ' and -
    while start > 0 and word(prompt[start - 1]):
        start -= 1
    while end < len(prompt) and word(prompt[end]):
        end += 1
    return dict(span, start=start, end=end, text=prompt[start:end])


def _markers(prompt, found, actions):
    """GLiNER2 markers that may be removed from the start of a request. The label also marks numbers and
    verbs ("0", "50", "double", "empty"): removing those deleted words from requests. A marker is kept only if
      - it isn't also an action ("double the stock"),
      - it has no digit (numbers are parameters; numbered lists are handled by LIST_START),
      - it is followed by punctuation or directly by an action ("First, delete", "Zuerst senke"),
        not by more words ("Last thing: …")."""
    starts = {a["start"] for a in actions}
    kept = []
    for m in (_whole_word(prompt, x) for x in found):
        if re.search(r"\d", m["text"]) or any(m["start"] < a["end"] and a["start"] < m["end"] for a in actions):
            continue
        after = m["end"] + len(prompt[m["end"]:]) - len(prompt[m["end"]:].lstrip())
        if prompt[after:after + 1] in (",", ":", ";") or after in starts:
            kept.append(m)
    return kept


def plan_cuts(prompt: str, model=None, spans=None):
    """Where to cut: one (start, end) per pair of actions, the spans trimmed from request edges (joiners and
    markers), and notes per cut."""
    spans = spans if spans is not None else find_spans(prompt, model)
    actions = spans["action"]
    # An action that starts inside a scope may be a verb of that scope's condition ("products that have less than
    # 5 units"): its cuts are still proposed, with a note for Jev.
    in_scope = [any(s["start"] < a["start"] < s["end"] for s in spans["scope"]) for a in actions]
    # A scope that overlaps an action starting before it is that action's object ("delete the products" +
    # "products under 12 dollars"), not a scope to protect between two actions.
    scopes = [s for s in spans["scope"]
              if not any(s["start"] < o["end"] and o["start"] < s["end"] for o in actions)]
    # a word also found as an action is a verb, never a joiner: trimming it lost "set" from "then set the TechSource
    # lead time…" (2026-10-10, case R18); _markers() applies the same rule to markers
    joiners = [j for j in spans["joiner"] if not _inside(j["start"], scopes)
               and not any(j["start"] < a["end"] and a["start"] < j["end"] for a in actions)]
    cuts, notes = [], []
    for i in range(len(actions) - 1):
        start, end, note = _choose_cut(prompt, actions[i]["end"], actions[i + 1]["start"], scopes, joiners, actions)
        cuts.append((start, end))
        notes.append([n for n in (note, NOTE_IN_SCOPE if in_scope[i] or in_scope[i + 1] else None) if n])
    # Spans trimmed from the edges of requests: markers first, so a word found as both is kept as a marker.
    edges = ([dict(m, kind="marker") for m in _markers(prompt, spans.get("marker", []), actions)]
             + [dict(j, kind="joiner") for j in joiners])
    return cuts, edges, notes


def assemble_detailed(prompt: str, cuts, edges) -> list[dict]:
    """The requests between the given cuts: {"text", "start", "end", "marker", "joined_by"}.

    marker     the sequence marker removed from the start of the request ("Second", "Finally", "2."), or None
    joined_by  the joining words between the previous request and this one ("and then", "after that"), or None
    start/end  where the text sits in the prompt
    """
    bounds = [(0, 0)] + list(cuts) + [(len(prompt), len(prompt))]
    requests, previous_end = [], 0
    for i, ((_, a), (b, _)) in enumerate(zip(bounds, bounds[1:])):
        start, end, marker = _bounds(prompt, a, b, edges, cut_before=i > 0, cut_after=i < len(bounds) - 2)
        if start >= end:
            continue
        gap = prompt[previous_end:start]
        if marker:
            gap = gap.replace(marker, " ", 1)
        else:                                    # a list number cut away from its item: "…to 4⟦\n2.⟧ set…"
            number = re.search(r"(?:^|\n)\s*(\d+[.)])\s*$", gap)
            marker = number.group(1) if number else None
        words = " ".join(re.findall(r"[^\W\d_][\w'’-]*", gap))
        requests.append({"text": prompt[start:end], "start": start, "end": end, "marker": marker,
                         "joined_by": words or None})
        previous_end = end
    return requests or [{"text": prompt, "start": 0, "end": len(prompt), "marker": None, "joined_by": None}]


def assemble(prompt: str, cuts, edges) -> list[str]:
    """The request texts between the given cuts."""
    return [r["text"] for r in assemble_detailed(prompt, cuts, edges)]


def split_requests_detailed(prompt: str, model=None, spans=None) -> list[dict]:
    """Offsets refer to the prompt as given: it is not stripped (request edges are trimmed instead)."""
    if not prompt.strip():
        return []
    cuts, edges, _ = plan_cuts(prompt, model, spans)
    return assemble_detailed(prompt, cuts, edges)


def split_requests(prompt: str, model=None, spans=None) -> list[str]:
    return [r["text"] for r in split_requests_detailed(prompt, model, spans)]
