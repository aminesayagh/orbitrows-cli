"""
The split with a Jev check: GLiNER2 proposes the cuts (split.py), Jev classifies each cut, and a cut is kept only
when Jev is confident it starts a new request. Moved from prompt_edit/split_check_cuts.py (2026-10-10), logic
unchanged; Jev through v2/model/client.py. Changes since (2026-10-10): CUT_MIN 0.80 (was 0.85); in the cut question,
a requirement to keep something as it is is a continuation, two actions that change different things are two
requests, and a phrase saying when a request runs belongs to it. Terms: docs/request-definition.md.

    .venv/bin/python -m v2.prompt_ast.check_cuts --dry-run "…"      # shows the Jev request, no call

Calls the paid Jev API: one call per prompt with at least one cut, one question per cut.

Why this shape: a request cut in two blocks the next step, two requests kept together don't. So Jev never
adds a cut; it can only confirm or cancel the ones GLiNER2 made, and anything but a confident
"new_request" keeps the pieces together:

    new_request         keep the cut, if its probability is at least CUT_MIN
    continuation        cancel the cut: the text after it belongs to the same request
    context_transition  cancel the cut: background text stays with the request it touches
    unknown             no answer reaches CUT_MIN: cancel the cut (the safe side)

A scope written once before several actions keeps them together ("for Samsung products, lower the price
and set the stock"): splitting them would leave the second action without its scope. Separating that scope
into its own request is a later step's decision.
"""

import json
import sys

from orbitrows.services.extractor import client as extractor
from orbitrows.services.model.client import ask
from .split import assemble, assemble_detailed, plan_cuts

# Run of 2026-10-01 on 199 cases: the highest new_request on a wrong cut was 0.77; every cut between 0.8 and 0.9 was
# right. 0.85 (user, 2026-10-01) keeps a 0.08 margin above the worst wrong cut; 0.8 gets more right but leaves 0.03.
# Merges don't block the next step; a split does.
# 0.80 (user, 2026-10-10), with the list rule and the "keep"/"and"/"when" rules: 199 old prompts 165 right, 0 instruction
# moved (one cut leaves "and I'd" on the wrong side, harmless); 24 challenge cases 18 right against 15 at 0.85.
CUT_MIN = 0.8

CRITERIA = {
    "new_request": "The text on each side of this cut is a request of its own: each side asks for a change or a "
                   "read of the table, with its own action. It is still a new request when the text after the cut "
                   "points back to the products before it ('them', 'their', 'its'): how the two are linked is "
                   "decided later.",
    "continuation": "The text after this cut belongs to the same request as the text before it: more of its targets, "
                    "its scope, its parameters, a constraint on it, or a value of it; a prohibition or a limit that "
                    "frames the request next to it ('never …', 'do not …', 'without …'); a requirement to keep or "
                    "preserve something as it is ('keep …', 'leave … unchanged', 'preserve …'); or another action that "
                    "shares a scope written once before both, without pointing back to it.",
    "context_transition": "The cut separates an instruction from text that asks for nothing: a greeting, an "
                          "explanation, thanks, a story about the past, a plan that was given up, or a statement of "
                          "fact.",
}
RULES = [
    # Examples must not quote test prompts (prompt_edit/test_requests.py, cases/), or the tests would grade Jev on what it was shown.
    "One action with several targets is one request: 'set the weight to 2 and the size to M for the Fjallraven bags'.",
    "Conditions, constraints ('but cap it at 300'), lists of products, sizes or values, and parameters such as "
    "'as preorder' stay with their request.",
    "When a scope written once before the cut applies to the actions on both sides, and the text after the cut does "
    "not point back to it ('for Lego sets, cut the price by 15% and set the stock to 8'), the cut is a continuation: "
    "splitting would leave an action without its scope.",
    # 2026-10-10 (challenge cases R01–R20): Jev kept "…and…" pairs of different changes together at 0.59–0.84
    "Two actions joined by 'and', '&', 'plus' or a comma that change different things (other columns, another "
    "table, or one edits while the other adds, deletes, archives or copies) are two requests, even when they act on "
    "the same products, unless a scope written once before both applies to them: in 'raise the Fender guitar prices "
    "by 5% & move their stock to the Lyon store', the cut is a new request. One action with several targets on the "
    "same rows ('set the weight to 2 and the size to M for the Fjallraven bags') stays one request.",
    "A phrase that only says when a request runs ('once that is done', 'as soon as the copy exists', 'after the "
    "update') asks for nothing itself and belongs to the request that follows it: the cut between the phrase and "
    "that request's action is a continuation, while the cut before the phrase, after another request, is a new "
    "request. In 'export the Fender guitars. Once that is done, hide them', the cut before 'Once' is a new request "
    "and a cut before 'hide' would be a continuation.",
    "When the text after the cut points back to the products before it ('…, then unpublish them', '…, and change "
    "their weight', '…, puis masque-les'), it is a new request: how the two are linked is decided later.",
    "A goal without a concrete change ('I want to reorganize my store') or a negated instruction ('leave the prices "
    "alone') is not a requested change.",
    "Creating something (a column, copies) and then changing what was created are always two requests, even in one "
    "sentence joined by 'and': in 'create a size column and fill it with M for the Puma shoes', the cut before 'fill' "
    "is a new request, not a value of the column.",
    "An action introduced by an order word ('but first', 'before that', 'beforehand', 'after that') is a new request "
    "that runs before or after the other one: in 'archive the Garmin watches, but first print their labels', the cut "
    "before 'but first' is a new request.",
    "A prohibition next to an instruction is a constraint of it, not a request of its own: in 'never touch the Asus "
    "stock; just rename the HP tablets', the cut is a continuation.",
    # 2026-10-10 (Orbitrow P100): "keep their category IDs" after a deletion was cut off as a request at 0.95
    "A requirement to keep something as it is, next to an instruction, is a constraint of it, not a request of its "
    "own: in 'hide the Garmin watches, but keep their prices, and keep their reviews visible', both cuts are "
    "continuations.",
    "A past event or an abandoned plan is context, not a request: in 'last week I meant to delete the Lenovo items. "
    "Today only hide them', the cut is a context transition.",
    # 2026-10-15 (operations step): an alternative formula cut off would apply both changes to its records
    "A formula given to some records IN PLACE OF the formula of the others ('…; the Lego sets get 20% off instead', "
    "'…, otherwise add 2', '…; for all the others, take 5 off', '…; the rest get 3') is a branch of the same "
    "change: the cut before it is a continuation (cut apart, those records would get both changes). Two changes "
    "of different records that do not replace each other ('add 3 to the Lego stock; the Duplo stock gets 2') may "
    "be two requests.",
    "A cut may carry `notes` from the splitter: evidence to weigh, not a decision.",
    "Judge only this cut; the other cuts are marked so you can see the whole prompt.",
]
CONTEXT = ("A merchant writes a prompt to edit their product catalog table. It may hold several requested changes, "
           "typos, and several languages. An automatic splitter proposed the cuts marked in `marked_prompt`; "
           "each cut must be confirmed or cancelled.")


def jev_request(prompt: str, cuts, notes=None):
    """The Jev state and one choice question per cut."""
    notes = notes or [[] for _ in cuts]
    marked, pos = [], 0
    for i, (start, end) in enumerate(cuts, 1):
        marked += [prompt[pos:start], f"⟦c{i}⟧"]
        pos = start
    marked.append(prompt[pos:])
    edges = [(0, 0)] + list(cuts) + [(len(prompt), len(prompt))]
    state = {
        "prompt": prompt,
        "marked_prompt": "".join(marked),
        "cuts": {f"c{i}": {"text_before": prompt[edges[i - 1][1]:start].strip(),
                           "separator": prompt[start:end],
                           "text_after": prompt[end:edges[i + 1][0]].strip(),
                           **({"notes": notes[i - 1]} if notes[i - 1] else {})}
                 for i, (start, end) in enumerate(cuts, 1)},
    }
    questions = {
        f"c{i}": {
            "type": "choice",
            "instructions": {
                "context": CONTEXT,
                "task": f"Considering the complete prompt, what is cut c{i}? The text on each side of it, up to the "
                        f"neighbouring cuts, is in `cuts.c{i}`.",
                "rules": RULES,
            },
            "criteria": CRITERIA,
        }
        for i in range(1, len(cuts) + 1)
    }
    return state, questions


async def ask_jev(jev, prompt: str, cuts, notes=None) -> list[dict]:
    """Jev's answer per cut: {"choice", "probabilities"}."""
    state, questions = jev_request(prompt, cuts, notes)
    a = await ask(jev, state, questions)
    return [{"choice": a[q][0], "probabilities": a[q][1]} for q in questions]


def decide(answer: dict, cut_min: float = CUT_MIN) -> str:
    """keep / continuation / context_transition / unknown."""
    if not answer["probabilities"]:              # Jev failed: no answer
        return "unknown"
    if answer["probabilities"].get("new_request", 0) >= cut_min:
        return "keep"
    best = max(answer["probabilities"], key=answer["probabilities"].get)
    return best if best != "new_request" and answer["probabilities"][best] >= cut_min else "unknown"


def apply_detailed(prompt: str, cuts, edges, answers, cut_min: float = CUT_MIN) -> list[dict]:
    """The requests after Jev's decisions: {"text", "start", "end", "marker", "joined_by"} (see assemble_detailed)."""
    kept = [c for c, a in zip(cuts, answers) if decide(a, cut_min) == "keep"]
    return assemble_detailed(prompt, kept, edges)


def apply(prompt: str, cuts, edges, answers, cut_min: float = CUT_MIN) -> list[str]:
    return [r["text"] for r in apply_detailed(prompt, cuts, edges, answers, cut_min)]


def reference_notes(cuts, references) -> list[list[str]]:
    """Step 1's links that cross a cut, as notes for Jev (decided 2026-10-02, option b): evidence that the text on
    one side acts on products named on the other side. Unresolved references are left out."""
    notes = [[] for _ in cuts]
    for r in references or []:
        if "target_start" not in r:
            continue
        for i, (start, end) in enumerate(cuts):
            if r["start"] >= end and r["target_end"] <= start:
                notes[i].append(f"'{r['text']}' after this cut points back to '{r['target']}' before it")
            elif r["end"] <= start and r["target_start"] >= end:
                notes[i].append(f"'{r['text']}' before this cut points to '{r['target']}' after it")
    return notes


async def split_checked(jev, prompt: str, model=None, references=None):
    """(requests, details): GLiNER2's cuts, Jev's answers, and the decision for each cut.

    If Jev fails, every cut is cancelled and details["unresolved"] is True: never fall back to GLiNER2's raw cuts.
    references: step 1's links (coref.py), added to the cuts' notes when they cross a cut."""
    if not prompt.strip():                       # not stripped: offsets refer to the prompt as given
        return [], {}
    cuts, joiners, notes = plan_cuts(prompt, model or extractor.connect())
    notes = [n + extra for n, extra in zip(notes, reference_notes(cuts, references))]
    unresolved = False
    try:
        answers = await ask_jev(jev, prompt, cuts, notes) if cuts else []
    except Exception as e:                       # API error, timeout: keep everything together
        answers, unresolved = [{"choice": "unknown", "probabilities": {}, "error": repr(e)} for _ in cuts], True
    details = {"cuts": cuts, "joiners": joiners, "notes": notes, "answers": answers, "unresolved": unresolved,
               "decisions": [decide(a) for a in answers], "gliner_requests": assemble(prompt, cuts, joiners)}
    details["requests"] = apply_detailed(prompt, cuts, joiners, answers)
    return [r["text"] for r in details["requests"]], details
