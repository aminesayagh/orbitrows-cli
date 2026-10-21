"""
Coreference. Finds the words of a prompt that point to other words ("them", "their", "the ones with no stock", "the
copies") and what they point to, before the prompt is split into requests (decided 2026-10-02). Moved from
prompt_edit/coref_requests.py (2026-10-10): logic, rules and thresholds unchanged; Jev goes through v2/model/client.py;
the store described in the context is a parameter (the default is the old one-table store).

The goal is the links between requests (for the link step: order and THEN / AND), never rewriting the prompt: the
next steps keep both the reference words and their target.

Option D of the benchmark (coref_bench.py, coref_cases.json):
    1. CorPipe 26 large (local GPU, .venv-corpipe) proposes the mentions: phrases, pronouns, clitics.
    2. Jev, one call per prompt, one question per mention: what does it point to? (another mention, nothing,
       new rows that an action creates, or ask the merchant).
    3. The uncertainty check comes first: a weak answer, whatever it is, leaves the mention unresolved (a warning).
CorPipe 26 is CC BY-NC-SA 4.0 (non-commercial): kept for the PoC, not for the product (notice.md #10).
"""

import os
import re
import select
import subprocess
from pathlib import Path

from orbitrows.services.model.client import ask
from .asking import question

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]                                       # the CLI folder: .venv-corpipe lives there
CORPIPE = ROOT / ".venv-corpipe" / "src" / "crac2026-corpipe" / "corpipe26_onestage.py"
CORPIPE_MODEL = "ufal/corpipe26-onestage-corefud1.4-large-260702"
JEV_MIN = 0.8
TARGET_SURE = 0.9          # a weak "not a reference" is overruled by a target this sure (benchmark: +1 right, no new error)
TOKEN = re.compile(r"[^\W_]+(?:['’][^\W_]+)?|-[^\W_]+|\S")

SERVER = HERE / "corpipe_server.py"
SERVER_WAIT = 300                                            # seconds: the first file also waits for the model load
_server = {}                                                 # model → the running server (one per process)


def run_corpipe(work: Path, name: str, model: str):
    """CorPipe on work/NAME.conllu → work/NAME.00.conllu. Through the kept-loaded server (corpipe_server.py); if it
    fails or hangs, the server is stopped and CorPipe runs once as before (loads the model: ~15 s more)."""
    python = str(ROOT / ".venv-corpipe" / "bin" / "python")
    p = _server.get(model)
    try:
        if p is None or p.poll() is not None:
            log = open(work / "corpipe_server.log", "a")
            p = _server[model] = subprocess.Popen([python, str(SERVER), model], cwd=work, text=True, stdin=subprocess.PIPE,
                                                  stdout=subprocess.PIPE, stderr=log)
        p.stdin.write(f"{name}.conllu\n")
        p.stdin.flush()
        while True:
            if not select.select([p.stdout], [], [], SERVER_WAIT)[0]:
                raise TimeoutError("CorPipe server: no answer")
            line = p.stdout.readline()
            if not line:
                raise RuntimeError("CorPipe server stopped")
            if line.startswith("@@corpipe ok"):
                return
            if line.startswith("@@corpipe error"):
                raise RuntimeError(line.strip())
    except (OSError, TimeoutError, RuntimeError):
        if p is not None:
            p.kill()
        _server.pop(model, None)
    subprocess.run([python, str(CORPIPE), "--load", model, "--exp", ".", "--epoch", "0", "--test", f"{name}.conllu"],
                   cwd=work, check=True, capture_output=True)


STORE = "their product catalog table (columns: sku, name, brand, price, stock, ean)"


def context(store: str = STORE) -> str:
    return f"A merchant wrote a prompt to edit or read {store}. Some words of the prompt only point to other words of the prompt."


CONTEXT = context()
RULES = ["A name repeated in full is not a reference to the earlier name: it names the products itself.",
         "'it' that points to nothing ('it is important') names nothing: it is not a reference.",
         "Words for the merchant or the assistant ('me', 'you', 'we', 'us'), values ('12', '20%'), column names and "
         "verbs are not references to other words of the prompt.",
         "'it' or 'them' after an action that creates something points to what that action creates; when the new "
         "thing is named, point to those words: in 'create a size column, then fill it with M', 'it' points to 'a "
         "size column'.",
         "A reference doesn't point to words of its own phrase that already play another part: in 'fill it with M "
         "for the Nike shoes', the Nike shoes are where the value goes, so 'it' is something else (here, a column).",
         "When the words could point to several of the listed words, choose that the merchant must be asked."]


def mentions(prompts: list[str], name: str = "corpipe", model: str = CORPIPE_MODEL) -> list[list[dict]]:
    """CorPipe's mentions for each prompt, in one run: [{start, end, text, cluster}], offsets in the prompt."""
    work = HERE / "coref_work"
    work.mkdir(exist_ok=True)
    tokens, lines = [], []
    for i, p in enumerate(prompts):
        toks = list(TOKEN.finditer(p))
        tokens.append([m.span() for m in toks])
        lines += [f"# newdoc id = d{i}", f"# sent_id = d{i}-1", "# text = " + " ".join(p.split())]   # one line
        for j, m in enumerate(toks, 1):
            misc = "SpaceAfter=No" if m.end() < len(p) and not p[m.end()].isspace() else "_"
            lines.append("\t".join([str(j), m.group(), "_", "_", "_", "_", "_", "_", "_", misc]))
        lines.append("")
    name = f"{name}_{os.getpid()}"                           # two processes at once never share a file
    (work / f"{name}.conllu").write_text("\n".join(lines) + "\n")
    run_corpipe(work, name, model)
    found, doc, k, stacks = [[] for _ in prompts], 0, 0, {}
    for line in (work / f"{name}.00.conllu").read_text().splitlines():
        if line.startswith("# newdoc id = d"):
            doc, k, stacks = int(line.split(" = d", 1)[1]), 0, {}
        if not line or line.startswith("#"):
            continue
        cols = line.split("\t")
        if not cols[0].isdigit():                            # an empty node (an implied subject): no words
            continue
        span = tokens[doc][k]
        k += 1
        entity = next((x[len("Entity="):] for x in cols[9].split("|") if x.startswith("Entity=")), "")
        for m in re.finditer(r"\((\w+)[^()]*?\)|\((\w+)[^()]*|(\w+)\)", entity):
            if m.group(1):
                found[doc].append((span[0], span[1], m.group(1)))
            elif m.group(2):
                stacks.setdefault(m.group(2), []).append(span[0])
            else:
                found[doc].append((stacks[m.group(3)].pop(), span[1], m.group(3)))
    return [[{"start": s, "end": e, "text": p[s:e], "cluster": eid} for s, e, eid in sorted(f)]
            for p, f in zip(prompts, found)]


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
            "unsure": f"It is not clear whether `{m['text']}` points to other words: the merchant must be asked."}}
        criteria = {f"m{j}": f"`{m['text']}` points to `{o['text']}`." for j, o in enumerate(ms)
                    if j != i and not (o["start"] <= m["start"] and m["end"] <= o["end"])}
        criteria["created"] = (f"`{m['text']}` points to new rows or columns that an action of the prompt creates, "
                               "and that are not named in the prompt (copies, duplicates).")
        criteria["ambiguous"] = f"`{m['text']}` could point to several words: the merchant must be asked."
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
    """The references of one prompt from its CorPipe mentions: {"references", "mentions", "answers"}. If Jev fails,
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
