"""The model behind every step: typed choices and yes-probabilities. Jev (TypeSafe System One) by default; MODEL in
the env picks another: a name in MODELS, or "package.module:Class" for a Model subclass kept outside this file.
Key from the env (TYPESAFE_API_KEY), set by the CLI launcher.

An answer below SURE becomes a question for the user: Recorded keeps every answer by call_id, settle() records the
user's answer as certain, and the import, run again, replays every recorded call without asking the model."""
import hashlib
import importlib
import json
import os

from typesafe_sdk import AsyncTypeSafeClient

SURE = 0.8                                                    # an answer counts at this probability; below, a warning
RETRIES = 2                                                   # a call that times out is retried twice (user, 2026-10-13)
FLOOR = 0.10                                                  # an option below this probability is not shown to the user
SHOWN = 4                                                     # at most this many options per question


class Model:
    """What a step talks to. A subclass answers the questions built by choice() and yes_no() below:
    {key: (choice, {option: probability})} for choices, {key: yes-probability} for yes/no questions,
    plus (input_tokens, output_tokens), None where unknown. Opened with `async with connect() as model`."""
    name = "model"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def answer(self, state, questions) -> tuple[dict, tuple]:
        raise NotImplementedError


class Jev(Model):
    name = "jev"

    async def __aenter__(self):
        self.client = await AsyncTypeSafeClient(api_key=os.environ["TYPESAFE_API_KEY"], model="jev-latest").__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.client.__aexit__(*exc)

    async def answer(self, state, questions):
        r = await self.client.system_one(state, questions)
        out = {k: (v.choice, dict(v.probabilities)) for k, v in (r.choices or {}).items()}
        out |= {k: v.noul for k, v in (getattr(r, "nouls", None) or {}).items()}
        usage = getattr(r, "usage", None)
        return out, (getattr(usage, "input_tokens", None), getattr(usage, "output_tokens", None))


MODELS = {"jev": Jev}


def call_id(state, questions) -> str:
    """The call's identity: the same state and questions give the same id, in any run."""
    text = json.dumps([state, questions], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()[:16]


class Answers(dict):
    """ask()'s answers, with `call`: the call_id they answer (None when there was no question)."""
    call = None


class Recorded(Model):
    """`inner`, with every answer kept in `calls` ({call_id: {key: [choice, {option: p}] or yes-probability}}, JSON-safe)
    and replayed from there: a run whose calls are all recorded never opens `inner`. `saved()` runs after each new call."""

    def __init__(self, inner: Model, calls: dict, saved=lambda: None):
        self.inner, self.calls, self.saved, self.opened = inner, calls, saved, False
        self.name = inner.name

    async def __aexit__(self, *exc):
        if self.opened:
            await self.inner.__aexit__(*exc)

    async def answer(self, state, questions):
        cid = call_id(state, questions)
        if cid in self.calls:
            return self.calls[cid], (None, None)
        if not self.opened:                                   # lazily: a replayed run needs no key and opens nothing
            await self.inner.__aenter__()
            self.opened = True
        out, usage = await self.inner.answer(state, questions)
        self.calls[cid] = {k: [v[0], dict(v[1])] if isinstance(v, (tuple, list)) else float(v) for k, v in out.items()}
        self.saved()
        return self.calls[cid], usage                         # the recorded form, so a replay sees exactly the same


def settle(calls: dict, call: str, key: str, value) -> None:
    """Records the user's answer to `key` of `call` as certain: a choice's option, or "yes"/"no". KeyError if unknown."""
    old = calls[call][key]
    calls[call][key] = [value, {value: 1.0}] if isinstance(old, (list, tuple)) else {"yes": 1.0, "no": 0.0}[value]


def options(probs: dict, labels: dict | None = None) -> list[dict]:
    """The options shown to the user, likeliest first: those at FLOOR or above, at most SHOWN, at least two.
    `labels` names opaque values (a letter for a column); "uncertain" is no answer the user can give."""
    labels = labels or {}
    ranked = sorted(((v, p) for v, p in probs.items() if v != "uncertain"), key=lambda vp: -vp[1])
    kept = [vp for vp in ranked if vp[1] >= FLOOR][:SHOWN]
    return [{"answer": labels.get(v, v), "value": v, "probability": round(p, 2)}
            for v, p in (kept if len(kept) >= 2 else ranked[:2])]


def connect(name=None) -> Model:
    """The model named by `name`, else MODEL from the env, else Jev."""
    name = name or (os.environ.get("MODEL") or "jev")
    if ":" in name:
        module, cls = name.split(":")
        return getattr(importlib.import_module(module), cls)()
    return MODELS[name]()


def choice(context, task, criteria, rules=(), **evidence):
    return {"type": "choice", "criteria": criteria,
            "instructions": {"context": context, "task": task, **evidence, "rules": list(rules)}}


def yes_no(context, task, rules=(), **evidence):
    return {"type": "noul", "instructions": {"context": context, "task": task, **evidence, "rules": list(rules)}}


async def ask(model, state, questions) -> Answers:
    """{key: (choice, {option: probability})} for choices, {key: yes-probability} for yes/no questions; .call names
    the call, for a question to the user."""
    if not questions:
        return Answers()
    for attempt in range(1 + RETRIES):                        # a timeout is retried, at most RETRIES times
        try:
            out, _ = await model.answer(state, questions)
            break
        except Exception as e:
            if "timeout" not in type(e).__name__.lower() or attempt == RETRIES:
                raise
    out = Answers(out)
    out.call = call_id(state, questions)
    return out


def sure(answers, key, at=SURE):
    """The chosen option when its probability reaches `at`, else None."""
    c, probs = answers.get(key, (None, {}))
    return c if c is not None and probs.get(c, 0) >= at else None


def pick(answers, key, column, what, warnings, labels=None, position=None):
    """(answer, flagged): the sure answer, else the likeliest one, flagged in `warnings` with a choice question for the
    user when the model answered. `labels` names opaque values in the question (options()); `position`, the column's
    id (its position in the file), goes in the warning next to its header `column`."""
    if c := sure(answers, key):
        return c, False
    top, probs = answers.get(key, (None, {}))
    w = {"column": column} | ({"position": position} if position is not None else {}) | {
         "problem": f"{what} uncertain", "answer": top,
         "probability": round(probs.get(top, 0), 2) if top else None}
    if top:                                                   # a model answer below SURE: the user picks
        w["question"] = {"type": "choice", "call": answers.call, "key": key, "options": options(probs, labels)}
    warnings.append(w)
    return top, True
