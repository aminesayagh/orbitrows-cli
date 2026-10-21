"""
Link the requests found by the split (split.py / check_cuts.py). Moved from prompt_edit/link_requests.py
(2026-10-10): the link question, rules, LINK_MIN and build() are unchanged; Jev goes through v2/model/client.py, and one
question per request is added in the same call: what the request takes from the request it waits for (INPUTS).
Terms: docs/request-definition.md.

Calls the paid Jev API: one call per prompt with two or more requests, two questions per request.

Model (decided 2026-10-01): a tree.
    THEN  a request waits for another one: they are steps, run in order.
    AND   requests that don't wait for each other: separate trees, or requests waiting for the same one.
          AND requests read one snapshot: the table as it was before them; their changes apply together. So a swap
          ("price into price TVA and price TVA into price") is AND, and gives the right result.
    Each request waits for at most one other request, so the links form a tree:
        "A, then B, and C"      (1 THEN 2) AND 3
        "A, then B and C"       1 THEN (2 AND 3)
        "A, then B, then C"     1 THEN 2 THEN 3
    Not expressible: a request waiting for two others ("combine the results of A and B").

Jev answers one question per request: "which request must this one wait for?", with every other request or
"none" as options. A request may wait for one written after it ("rename the copies, but first duplicate the Dell
mice"), so the answers give both the execution order and the tree. The examples in RULES must not
be test prompts (prompt_edit/test_requests.py LINK_CASES, cases/), or the tests would grade Jev on what it was shown. The words ("then", "and", "first") are
evidence for Jev, not rules: "then" sometimes means AND.

Step 2 translates the order the merchant meant (decided 2026-10-01/02). THEN: the request that runs first changes the
state the other one uses; or the merchant sets an order ("first", "before", ordinal markers), followed in that
direction; or the merchant writes an order ("then") between requests on the same products ("lower the Bose prices,
then delete them"). AND: requests on different products, even with "then", or a plain "and". The one exception: a
swap or rotation is AND, even with "then". Which products "them" means is resolved by a later scope step.

A link below LINK_MIN, or a cycle, is not guessed: it is reported in "warnings", so the user can be asked.
"""

from orbitrows.services.model.client import ask

LINK_MIN = 0.8             # 0 wrong links above it in the 2026-10-01 runs; --link-replay sweeps it on saved answers
NONE = "none"
# What a request takes from the request it waits for (2026-10-10): it decides the SQL (guide §11–12). "rows" becomes
# request_result (the earlier request reads) or affected_rows (it changes) once the operation is known; "structure"
# is the AST's schema_effect; "table_state" runs on the table as the earlier request leaves it.
INPUTS = {
    "rows": "It acts on or reads the rows that the request it waits for returned, selected, created or changed: it "
            "points back to them ('them', 'those products', 'the copies', 'the updated ones').",
    "structure": "It uses a column or a table that the request it waits for creates or renames.",
    "table_state": "It works on the table as the request it waits for leaves it, without pointing back to that "
                   "request's rows: it only needs that request to be done first.",
}
INPUT_RULES = [
    "Answer as if the request waits for another one, even if you think it does not; another question decides that.",
    "'them', 'their' or 'those' pointing to the products of the earlier request is 'rows': in 'list the Lego sets "
    "under 20, then hide them', hiding takes the rows of the list.",
    "A new condition on the whole table, even on values the earlier request changed, is 'table_state': in 'raise "
    "every Garmin price by 5%, then list the watches above 300', the list reads the whole table again.",
    "A column or table named by the earlier request as new or renamed is 'structure': in 'add a gift_wrap column, "
    "then set it to yes for the Lego sets', the second request uses the new column.",
]

CONTEXT = ("A merchant wrote a prompt to edit their product catalog table. It was split into the separate requests "
           "in `requests`, in the order they are written. They must now be ordered and linked: a request either "
           "waits for another one to be done first, or runs independently.")
RULES = [
    "A request waits for another one when the one that runs first changes the state the other one uses: it works on "
    "rows or columns the first one creates ('duplicate the Lenovo tablets, then rename the duplicates'), or reads "
    "values or rows after the first one changed them ('raise the cost by 3%, then compute the margin from the new "
    "cost', 'set the Asus stock to 0, then list the products without stock').",
    "When the merchant explicitly sets an order ('first', 'before', 'after that', 'once that is done', or a sequence "
    "of ordinal markers like 'First … Then …', 'Firstly … Secondly …'), follow it, in that direction, even when the "
    "requests are on different products: the merchant asked for that order. In 'archive the old Nokia phones, but "
    "before that print their list', the archiving waits for the printing; in 'Firstly, hide the Acer tablets. "
    "Secondly, list the HP printers.', the listing waits for the hiding. The request it waits for can be written after it.",
    "When the merchant writes an order ('then', 'after that', 'puis', 'luego') between requests on the same "
    "products, follow it: the order is what the merchant asked for. Requests are on the same products when the later "
    "one refers back ('them', 'their', 'for those same items') or names the same products again: in 'tag the Asus "
    "laptops as clearance, then archive them', the archiving waits for the tagging.",
    "'them' or 'their' after a request that creates products (copies, duplicates) means the new products: that "
    "request waits.",
    "Requests on different products don't wait for each other, even when written with 'then': 'lower the Asus prices, "
    "then set the HP stock to 5' are independent, and so are requests joined by a plain 'and' with no order and no "
    "change the other one uses.",
    "The one exception to a written order is a swap or a rotation: request A copies column X into column Y, and "
    "request B copies column Y back into column X ('put the width into the height, then the height into the "
    "width'). The merchant wants the two values exchanged, so each request reads the values from before and neither "
    "waits, even when written with 'then': if B waited, it would copy back the value A just wrote and both columns "
    "would end up equal.",
    "'then' and 'and' (in `joined_by`) are evidence, not decisions; ordinal markers (`marker`: 'First', 'Next', "
    "'Finally') state an order.",
    "`refers_to` (when present) lists the words of a request that point to the products of another request, found "
    "by an earlier step: the two requests are on the same products.",
    "Answer for this request only; a chain of steps is built from the answers for every request.",
]


def jev_request(prompt: str, requests: list[dict], references: list[dict] | None = None):
    """The Jev state and one choice question per request (ids r1, r2, …). references: step 1's links
    (coref_requests.by_request), shown on the request they are in."""
    ids = [f"r{i}" for i in range(1, len(requests) + 1)]
    state = {
        "prompt": prompt,
        "requests": {rid: {k: r.get(k) for k in ("text", "marker", "joined_by") if r.get(k)}
                     for rid, r in zip(ids, requests)},
    }
    for ref in references or []:
        if ref.get("request") and ref.get("target_request") and ref["target_request"] != ref["request"]:
            state["requests"][f"r{ref['request']}"].setdefault("refers_to", []).append(
                f"'{ref['text']}' points to '{ref['target']}' in r{ref['target_request']}")
        elif ref.get("request") and ref["kind"] == "created":
            state["requests"][f"r{ref['request']}"].setdefault("refers_to", []).append(
                f"'{ref['text']}' points to new rows that another request creates")
    questions = {}
    for rid, r in zip(ids, requests):
        criteria = {other: f"Request {other} ('{req['text']}') must be done before request {rid}."
                    for other, req in zip(ids, requests) if other != rid}
        criteria[NONE] = f"Request {rid} doesn't wait for any other request: it can run independently."
        questions[rid] = {
            "type": "choice",
            "instructions": {
                "context": CONTEXT,
                "task": f"Considering the complete prompt, which request must request {rid} ('{r['text']}') wait for?",
                "rules": RULES,
            },
            "criteria": criteria,
        }
    return state, questions


def input_questions(requests: list[dict]) -> dict:
    """One choice question per request: what it takes from the request it waits for (asked in the link call)."""
    return {f"r{i}_input": {
        "type": "choice",
        "instructions": {"context": CONTEXT, "task": f"If request r{i} ('{r['text']}') waits for another request, "
                                                     f"what does it take from it?", "rules": INPUT_RULES},
        "criteria": INPUTS} for i, r in enumerate(requests, 1)}


async def ask_jev(jev, prompt: str, requests: list[dict], references: list[dict] | None = None):
    """(link answers, input answers, call): per request {"choice", "probabilities"} for the link, and
    {"r1": (choice, probabilities), …} for what it takes from the request it waits for; the call, for a question."""
    state, questions = jev_request(prompt, requests, references)
    a = await ask(jev, state, questions | input_questions(requests))
    return ([{"choice": a[q][0], "probabilities": a[q][1]} for q in questions],
            {q.removesuffix("_input"): a[q] for q in a if q.endswith("_input")}, a.call)


def build(requests: list[dict], answers: list[dict], link_min: float = LINK_MIN) -> dict:
    """The tree of links and the warnings, from Jev's answers.

    tree      root nodes {"request": number, "then": [nodes that wait for it]}; roots and siblings are joined by AND
    links     the tree as text: "(1 THEN 2) AND 3", "1 THEN (2 AND 3)"
    waits_for per request (1-based), the request number it waits for, or None
    warnings  links below link_min, invalid answers, and cycles: the user should be asked
    """
    n, warnings = len(requests), []
    if len(answers) > n:
        raise ValueError(f"{len(answers)} answers for {n} requests")
    waits_for = [None] * n
    for i, answer in enumerate(list(answers) + [{}] * (n - len(answers))):
        probs = (answer.get("probabilities") if isinstance(answer, dict) else None) or {}
        best = max(probs, key=probs.get) if probs else None
        if best is None or probs[best] < link_min:
            warnings.append({"request": i + 1, "problem": "uncertain link",
                             "best": best, "probability": probs.get(best) if best else None})
            continue                                         # not guessed: treated as independent, with a warning
        if best == NONE:
            continue
        target = int(best[1:]) - 1 if best[:1] == "r" and best[1:].isdigit() else None
        if target is None or not 0 <= target < n or target == i:   # an unknown request, or itself: not guessed
            warnings.append({"request": i + 1, "problem": "invalid answer", "best": best})
            continue
        waits_for[i] = target
    # a cycle ("1 waits for 2, 2 waits for 1") can't be ordered: drop its links and warn
    for i in range(n):
        seen, j = set(), i
        while j is not None and j not in seen:
            seen.add(j)
            j = waits_for[j]
        if j is not None:                                    # came back to a visited request: cycle through j
            cycle, k = [], j
            while k not in cycle:
                cycle.append(k)
                k = waits_for[k]
            for k in cycle:
                waits_for[k] = None
            warnings.append({"requests": sorted(k + 1 for k in cycle), "problem": "cycle"})
    followers = {i: [] for i in range(n)}
    roots = []
    for i, j in enumerate(waits_for):
        (roots if j is None else followers[j]).append(i)

    def node(i):
        return {"request": i + 1, "then": [node(k) for k in followers[i]]}

    tree = [node(i) for i in roots]
    return {"tree": tree, "links": expression(tree),
            "waits_for": [j + 1 if j is not None else None for j in waits_for], "warnings": warnings}


async def link(jev, prompt: str, requests: list[dict], references: list[dict] | None = None) -> dict:
    """The links of one prompt. One request: no call. If Jev fails: every link is a warning. "call": the call's id,
    for a question to the user (None when there was no call)."""
    if len(requests) < 2:
        tree = [{"request": 1, "then": []}] if requests else []
        return {"tree": tree, "links": expression(tree), "waits_for": [None] * len(requests), "warnings": [],
                "answers": [], "inputs": {}, "call": None}
    try:
        answers, inputs, call = await ask_jev(jev, prompt, requests, references)
    except Exception as e:                                   # API error, timeout: nothing is guessed
        answers, inputs, call = [{"choice": None, "probabilities": {}, "error": repr(e)} for _ in requests], {}, None
    return {**build(requests, answers), "answers": answers, "inputs": inputs, "call": call}


def expression(tree: list[dict]) -> str:
    """The tree as text, in written order: '(1 THEN 2) AND 3', '1 THEN (2 AND 3)', '1 THEN 2 THEN 3'."""
    def wrap(text):
        return f"({text})" if " " in text else text

    def show(node):
        follow = node["then"]
        if not follow:
            return str(node["request"])
        if len(follow) == 1:
            return f"{node['request']} THEN {show(follow[0])}"
        return f"{node['request']} THEN (" + " AND ".join(wrap(show(f)) for f in follow) + ")"

    return " AND ".join(wrap(show(n)) if len(tree) > 1 else show(n) for n in tree)
