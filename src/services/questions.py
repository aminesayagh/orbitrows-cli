"""A question for the user, shared by store imports (q_…/n_…), incoming uploads (i_…), and messages (m_…). Their
states live in import.json, incoming_pending/<upload>/progress.json, and the session server's memory respectively. A question is
a warning with {"id", "question": {"type": choice | yes_no | name | formula, …}}; a state is {"open", "answered",
"calls", "step", …}. Light on imports: the session server loads this at its start."""


class Invalid(Exception):
    """An answer refused: its text lists what can be given."""


def fields(q: dict, keys: tuple, nulls: bool) -> dict:
    """The question's `keys`: missing ones null when `nulls` (an import's JSON), else left out (a message's)."""
    return {k: q.get(k) for k in keys if nulls or k in q}


def shown(q: dict, keys: tuple, nulls: bool) -> dict:
    """A question as the user sees it: its options' answers and probabilities, not the call its answer settles."""
    ask = q["question"]
    return (fields(q, keys, nulls) | {"type": ask["type"]}
            | ({"options": [{k: o[k] for k in ("answer", "probability", "preview") if k in o}
                            for o in ask["options"]]}
               if ask["type"] == "choice" else {})                    # a yes/no is answered yes or no
            | ({"default": ask["default"]} if ask["type"] == "name" else {}))   # a name: any text, or skip


def asking(state: dict, notices: list, keys: tuple, nulls: bool) -> dict:
    """The state's open questions, as the user sees them, with the new notices."""
    return {"step": state["step"], "questions": [shown(q, keys, nulls) for q in state["open"]], "notices": notices}


def answers(question: dict) -> dict:
    """A choice's or a yes/no's valid answers as the user gives them (an option's answer, or yes/no) → the value
    client.settle() takes."""
    return ({str(o["answer"]): o["value"] for o in question["options"]} if question["type"] == "choice"
            else {"yes": "yes", "no": "no"})


def find(state: dict, question: str | None, given: str | None, where: str, none: str) -> dict:
    """The open question `question` of the state, if an answer is given; else Invalid: its open questions' ids
    (`where` names the state's owner) or, none open, `none`."""
    if given is not None and (q := next((q for q in state["open"] if q["id"] == question), None)):
        return q
    ids = ", ".join(q["id"] for q in state["open"])
    raise Invalid(f"Give an open question's id and its answer. Open questions{where}: {ids}" if ids else none)


def match(q: dict, given: str) -> str:
    """The answer as recorded: a valid answer matched case-insensitively; for a name, the text as a SQL name or, on
    skip, the default; for a formula, the text parse() accepts. Else Invalid, listing the valid answers."""
    ask = q["question"]
    if ask["type"] == "formula":                              # a unit conversion: any formula from x parse() takes
        from orbitrows.pipeline.merge.convert import parse    # here: pydantic, as for a name
        try:
            parse(given)
        except ValueError as e:
            raise Invalid(str(e)) from None
        return given.strip()
    if ask["type"] == "name":                                 # a table split out of a file: any name, or skip
        from orbitrows.sql.ddl import sql_name                # here: pydantic, which a message's answer doesn't need
        return ask["default"] if given.casefold() == "skip" else sql_name(given)   # never a leading _: the store's own
    valid = answers(ask)
    if (found := next((a for a in valid if a.casefold() == given.casefold()), None)) is None:
        raise Invalid(f"{given} isn't an answer to {q['id']}. Answers: {', '.join(valid)}")
    return found


def record(state: dict, q: dict, answer: str, keys: tuple, nulls: bool) -> None:
    """The answer, matched, moves q from open to answered; a model's question (with a call) has it settled in the
    state's calls, as certain. Other questions (a name, a confirmation) are the caller's to act on."""
    ask = q["question"]
    if "call" in ask:
        from orbitrows.services.model.client import settle   # here: the model's SDK, loaded only once answering
        settle(state["calls"], ask["call"], ask["key"], answers(ask)[answer])
    state["open"].remove(q)
    state["answered"].append(fields(q, keys, nulls) | {"answer": answer})
