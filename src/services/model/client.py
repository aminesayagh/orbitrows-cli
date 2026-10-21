"""The model behind every step: typed choices and yes-probabilities. Jev (TypeSafe System One) by default; MODEL in
the env picks another: a name in MODELS, or "package.module:Class" for a Model subclass kept outside this file.
Key from the env (TYPESAFE_API_KEY), set by the CLI launcher."""
import importlib
import os

from typesafe_sdk import AsyncTypeSafeClient

SURE = 0.8                                                    # an answer counts at this probability; below, a warning
RETRIES = 2                                                   # a call that times out is retried twice (user, 2026-10-13)


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


async def ask(model, state, questions) -> dict:
    """{key: (choice, {option: probability})} for choices, {key: yes-probability} for yes/no questions."""
    if not questions:
        return {}
    for attempt in range(1 + RETRIES):                        # a timeout is retried, at most RETRIES times
        try:
            out, _ = await model.answer(state, questions)
            break
        except Exception as e:
            if "timeout" not in type(e).__name__.lower() or attempt == RETRIES:
                raise
    return out


def sure(answers, key, at=SURE):
    """The chosen option when its probability reaches `at`, else None."""
    c, probs = answers.get(key, (None, {}))
    return c if c is not None and probs.get(c, 0) >= at else None

