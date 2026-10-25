"""Phase 4: selection. The search's candidates (index.py, the top K by similarity) are shown to Jev with what each
does and how it differs from the others; Jev chooses one, or none (no entry does what the request asks: the honest
"not supported"). Similarity ranks, Jev decides: similarity alone cannot refuse (an unsupported request scores as
high as a supported one, evaluation.py).

    → {"status": "resolved" | "needs_clarification" | "unsupported", "operation"?: id, "probability",
       "candidates": [ids]}; below SURE the likeliest answer is kept and the request needs clarification
"""
from orbitrows.services.model.client import SURE, ask, choice

K = 10
CONTEXT = ("A program (often an AI agent) asked a command-line tool to read or change a table of a SQLite database. "
           "The tool knows a catalog of operations; the candidates are the closest ones. Choose the one the request "
           "asks for, or none.")
RULES = [
    "Choose an operation only when it does what the request asks, with the request's own values and columns; a "
    "similar but different operation is not the request's.",
    "Choose none when no candidate does it (a median, a percentile, a ranking, a translation are none unless a "
    "candidate is exactly that).",
    "The operation is the computation or the change; which table, which records and which columns are not part of "
    "the choice.",
    "Showing, deleting or updating records is decided elsewhere: in 'remove the tickets closed over a year ago' the "
    "operation is the condition (closed over a year ago), in 'list the rows whose score beats the class mean' it is "
    "the comparison with the mean.",
]


def described(entry: dict) -> str:
    return f"{entry['name']}: {entry['behavior']} " + " ".join(entry["distinctions"])


def questions(requests: dict, candidates: dict, entries: dict) -> dict:
    """{key: choice}, one per request ({key: its text}), among its candidates ({key: [ids]}) and none."""
    return {key: choice(CONTEXT, f"Which operation does this request ask for: '{text}'?",
                        {i: described(entries[i]) for i in candidates[key]}
                        | {"none": "None of these: the request asks for something else."}, RULES)
            for key, text in requests.items()}


def decided(answer, candidates: list[str]) -> dict:
    op, probs = answer if answer else (None, {})
    p = round(probs.get(op, 0), 2)
    if op is None:
        return {"status": "needs_clarification", "probability": 0, "candidates": candidates}
    if op == "none":
        return {"status": "unsupported" if p >= SURE else "needs_clarification", "probability": p,
                "candidates": candidates}
    return {"status": "resolved" if p >= SURE else "needs_clarification", "operation": op, "probability": p,
            "candidates": candidates}


async def select(jev, index, entries: dict, requests: dict) -> dict:
    """{key: decided()} for every request ({key: its text}), in one call."""
    candidates = {key: [i for i, _ in index.retrieve(text, K)] for key, text in requests.items()}
    answers = await ask(jev, {}, questions(requests, candidates, entries))
    return {key: decided(answers.get(key), candidates[key]) for key in requests}
