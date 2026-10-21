"""The prompt steps' questions for the user (not a PoC module): a warning built from a model answer below its step's
threshold carries {"question": …}, as the store import's (model.client.pick), and pipeline.run stops on it (Paused);
every other warning is a notice. After client.settle(), the replay sees the user's answer as certain (1.0 / 0.0)."""
import re

from orbitrows.services.model.client import options

ID = re.compile(r"t_(\w+?)(?:\.c_(\w+))?(=.*)?")              # the store's ids (ddl.schema_from_sqlite): t_<table>.c_<col>


def question(answers, key: str, labels: dict | None = None, skip=()) -> dict:
    """{"question": …} to merge into a warning on `key` of `answers` (ask()'s): a choice with its options, the store's
    ids named table.column and `labels` naming other opaque values, `skip` the options that ask the user back
    ("ambiguous", "unclear"); or a yes/no. {} when the model gave no answer (an error: no call)."""
    call, v = getattr(answers, "call", None), answers.get(key)
    if call is None or v is None:
        return {}
    if not isinstance(v, (list, tuple)):
        return {"question": {"type": "yes_no", "call": call, "key": key}}
    probs = {o: p for o, p in v[1].items() if o not in skip}
    if not probs:
        return {}
    names = {o: m[1] + (f".{m[2]}" if m[2] else "") + (m[3] or "") for o in probs if (m := ID.fullmatch(o))}
    return {"question": {"type": "choice", "call": call, "key": key, "options": options(probs, names | (labels or {}))}}
