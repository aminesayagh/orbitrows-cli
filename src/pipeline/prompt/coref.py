"""
Coreference. Finds the words of a prompt that point to other words ("them", "their", "the ones with no stock", "the
copies") and what they point to, before the prompt is split into requests (decided 2026-10-02). Moved from
prompt_edit/coref_requests.py (2026-10-10): logic, rules and thresholds unchanged; Jev goes through v2/model/client.py;
the store described in the context is a parameter (the default is the old one-table store).

The goal is the links between requests (for the link step: order and THEN / AND), never rewriting the prompt: the
next steps keep both the reference words and their target.

    1. spaCy (the CLI's English model) proposes the mentions, by grammar only: every noun phrase at its whole extent
       from its own first word ("the price of the Bose speakers", never its bare "the price", never "more than twice
       their cost"), every subject or object whose head is a word but no noun
       ("that one", "the cheapest one", "the former"), every pronoun or possessive but the relative ones.
    2. Jev, one call per prompt, one question per mention: what does it point to? (another mention, nothing,
       new rows that an action creates, or ask the user).
    3. The uncertainty check comes first: a weak answer, whatever it is, leaves the mention unresolved (a warning).
Replaced CorPipe 26 large on 2026-10-27 (its license, CC BY-NC-SA, is non-commercial; 8.6 GB with its environment).
Benchmark, the PoC's 70 English prompts (62 references), Jev deciding each source's mentions (.bench/coref_compare.py):
spaCy 50 right, 1 wrong, 1 silent, 1 false link; CorPipe large 51 / 1 / 3 / 0; f-coref 22 / 1 / 33 / 0 (it only
proposes mentions it has linked). Adding GLiNER2's candidates offers no more of the references (61/62 either way).
"""

from orbitrows.pipeline.expressions.words import nlp
from orbitrows.services.model.client import ask
from .asking import question

JEV_MIN = 0.8
TARGET_SURE = 0.9          # a weak "not a reference" is overruled by a target this sure (benchmark: +1 right, no new error)
ARGUMENTS = ("dobj", "nsubj", "nsubjpass", "pobj", "attr")    # spaCy's dependency labels of a subject or an object
RELATIVE = ("WDT", "WP")                                      # spaCy's tags of a relative pronoun ("that", "which")


STORE = "their product catalog table (columns: sku, name, brand, price, stock, ean)"


def context(store: str = STORE) -> str:
    return f"A user wrote a prompt to edit or read {store}. Some words of the prompt only point to other words of the prompt."


CONTEXT = context()
RULES = ["A name repeated in full is not a reference to the earlier name: it names the products itself.",
         "'it' that points to nothing ('it is important') names nothing: it is not a reference.",
         "Words for the user or the assistant ('me', 'you', 'we', 'us'), values ('12', '20%'), column names and "
         "verbs are not references to other words of the prompt.",
         "'it' or 'them' after an action that creates something points to what that action creates; when the new "
         "thing is named, point to those words: in 'create a size column, then fill it with M', 'it' points to 'a "
         "size column'.",
         "A reference doesn't point to words of its own phrase that already play another part: in 'fill it with M "
         "for the Nike shoes', the Nike shoes are where the value goes, so 'it' is something else (here, a column).",
         "When the words could point to several of the listed words, choose that the user must be asked."]


def mentions(prompts: list[str]) -> list[list[dict]]:
    """The mentions of each prompt, by spaCy's grammar: [{start, end, text}], offsets in the prompt, in text order."""
    out = []
    for p in prompts:
        doc, spans = nlp("en")(p), set()
        for chunk in doc.noun_chunks:                         # its whole extent only; a relative pronoun is
            if chunk.root.tag_ in RELATIVE:                   # no mention ("every product that has": no "that")
                continue
            whole = doc[chunk.start:chunk.root.right_edge.i + 1]   # from its own first word: never "more than
                                                              # twice their cost", "under 40% of their price"
            spans.add((whole.start_char, whole.end_char))
        for t in doc:
            if t.dep_ in ARGUMENTS and t.is_alpha and t.pos_ not in ("NOUN", "PROPN", "PRON", "VERB", "AUX"):
                whole = doc[t.left_edge.i:t.right_edge.i + 1]   # a word heading no noun phrase ("one", "former")
                spans.add((whole.start_char, whole.end_char))
            if (t.pos_ == "PRON" or t.tag_ == "PRP$") and t.tag_ not in RELATIVE:   # not "the one that has"
                spans.add((t.idx, t.idx + len(t)))
        out.append([{"start": a, "end": b, "text": p[a:b]} for a, b in sorted(spans)])
    return out


def jev_request(prompt: str, ms: list[dict], ctx: str = CONTEXT):
    """The Jev state and two choice questions per mention (decided 2026-10-02): its role (mN_role: a reference or
    not) and its target (mN_target: which words). Jev answers them independently; decide() reconciles them."""
    questions = {}
    for i, m in enumerate(ms):
        questions[f"m{i}_role"] = {"type": "choice", "instructions": {
            "context": ctx, "task": f"In the prompt, is `{m['text']}` a reference to other words of the prompt?",
            "rules": RULES}, "criteria": {
            "reference": f"`{m['text']}` doesn't say itself what it means: it points to other words of the prompt "
                         "(like 'them', 'their', 'the ones with no stock', 'the copies', 'the latter').",
            "not_reference": f"`{m['text']}` names products, a column, a value, a person or an action itself: it is not "
                             "a reference.",
            "unsure": f"It is not clear whether `{m['text']}` points to other words: the user must be asked."}}
        criteria = {f"m{j}": f"`{m['text']}` points to `{o['text']}`." for j, o in enumerate(ms)
                    if j != i and not (o["start"] <= m["start"] and m["end"] <= o["end"])}
        criteria["created"] = (f"`{m['text']}` points to new rows or columns that an action of the prompt creates, "
                               "and that are not named in the prompt (copies, duplicates).")
        criteria["ambiguous"] = f"`{m['text']}` could point to several words: the user must be asked."
        questions[f"m{i}_target"] = {"type": "choice", "criteria": criteria, "instructions": {
            "context": ctx, "task": f"If `{m['text']}` is a reference, what does it point to?", "rules": RULES}}
    return {"prompt": prompt}, questions


async def ask_jev(jev, prompt: str, ms: list[dict], store: str = STORE) -> dict:
    """{mention id: (choice, probabilities)}; no call when there is no mention."""
    if not ms:
        return {}
    state, questions = jev_request(prompt, ms, context(store))
    return await ask(jev, state, questions)


def decide(ms: list[dict], answers: dict) -> list[dict]:
    """Jev's answers → references. The role answer only drops a mention that is clearly not a reference (>= JEV_MIN);
    otherwise the target answer decides, with the same check: weak, missing or "ask" → unresolved (kind "ambiguous":
    a warning); confident → a link. Noun-phrase references ("the speakers") get a weak role but a sure target. Answers saved before the role question (one question per mention) still work.
    An unresolved reference carries the question on its target for the user (asking.py), labelled with the mentions."""
    asked, labels = answers, {f"m{j}": o["text"] for j, o in enumerate(ms)} | {
        "created": "new rows or columns the prompt creates"}
    roles = any(q.endswith("_role") for q in answers)
    if roles:
        merged = {}
        for i in range(len(ms)):
            role, p_role = answers.get(f"m{i}_role", (None, {}))
            target, p_target = answers.get(f"m{i}_target", (None, {}))
            p_r = p_role.get(role, 0.0)
            if role == "not_reference" and p_r >= JEV_MIN:     # clearly not a reference: no link, no warning
                merged[f"m{i}"] = ("self", {"self": p_r})
            elif role in ("reference", "unsure") or p_target.get(target, 0.0) >= TARGET_SURE:
                # possibly a reference (or a weak "not a reference" with a very sure target): the target decides
                merged[f"m{i}"] = (target, {target: p_target.get(target, 0.0)})
            else:                                           # weak "not a reference", or no answer: unresolved
                merged[f"m{i}"] = ("ambiguous", {"ambiguous": p_r})
        answers = merged
    refs = []
    for q, (choice, probs) in answers.items():
        m = ms[int(q[1:])]
        p = probs.get(choice, 0.0)
        base = {"start": m["start"], "end": m["end"], "text": m["text"], "score": round(p, 3)}
        if p < JEV_MIN or choice == "ambiguous" or choice is None:
            refs.append({**base, "kind": "ambiguous", "target": ""}
                        | question(asked, f"{q}_target" if roles else q, labels, skip=["ambiguous"]))
        elif choice == "created":
            refs.append({**base, "kind": "created", "target": ""})
        elif choice != "self":
            t = ms[int(choice[1:])]
            refs.append({**base, "kind": "forward" if t["start"] > m["start"] else "existing", "target": t["text"],
                         "target_start": t["start"], "target_end": t["end"]})
    return refs


async def resolve(jev, prompt: str, ms: list[dict], store: str = STORE) -> dict:
    """The references of one prompt from its mentions: {"references", "mentions", "answers"}. If Jev fails,
    every mention is unresolved."""
    error = None
    try:
        answers = await ask_jev(jev, prompt, ms, store)
    except Exception as e:                                   # API error: nothing is guessed
        answers, error = {f"m{i}_role": (None, {}) for i in range(len(ms))}, repr(e)
    return {"references": decide(ms, answers), "mentions": ms, "answers": answers, **({"error": error} if error else {})}


def by_request(refs: list[dict], requests: list[dict]) -> list[dict]:
    """Each reference with the request it is in and the request its target is in (1-based, None if outside)."""
    def where(s, e):
        return next((i for i, r in enumerate(requests, 1) if r["start"] <= s and e <= r["end"]), None)
    return [{**r, "request": where(r["start"], r["end"]),
             "target_request": where(r["target_start"], r["target_end"]) if "target_start" in r else None} for r in refs]
