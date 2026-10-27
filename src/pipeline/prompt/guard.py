"""The guardrail in front of the pipeline (notice #15): a message that is not a request about the store's data (undo a
change, save the session, export or back up the files, a question about the tool, anything unrelated) stops before
any step runs, with the CLI command that does it, said for the client (an agent). One Jev question; below SURE, or
on an error, the pipeline runs: a data request is never stopped on a doubt (step 2's "application" is the second net).

    run(model, prompt) → None (the pipeline runs) or the message saying what to do instead
"""
from orbitrows.services.model.client import SURE, ask, choice

CONTEXT = "A message was sent to a command-line tool that reads and changes a store's database (its tables of data)."
KINDS = {
    "data": "It asks to read, find, count, compare, change, add, remove or copy the store's data or tables, at least "
            "in part ('show the variants under 100', 'copy the suppliers into a table called suppliers_old').",
    "undo": "It asks to undo or revert a change already made, as a whole ('undo the last change', 'cancel what you "
            "just did'), not to set values to something given.",
    "exit": "It asks to save the changes made so far, or to end or close the session.",
    "export": "It asks to export or download the store's files.",
    "backup": "It asks for a backup or a safety copy with no table named to copy into, or to restore an earlier "
              "version of the store.",
    "tool": "It asks how the tool works or how to do something with it ('how do I add a supplier file?').",
    "unrelated": "It is about something else than the store's data or the tool.",
}
RULES = ["A message that also asks for something else than the data ('take a backup, then delete …') is that other "
         "thing: the data part cannot run before it."]
SAY = {
    "undo": "to revert the session's last change, run `orbitrows undo`",
    "exit": "to save the session's changes and end it, run `orbitrows exit`",
    "export": "to write the store's files into a folder, run `orbitrows export -o <folder>`",
    "backup": "a backup or a restore is the tool's, not the database's: `orbitrows export -o <folder>` writes a copy of "
              "the store's files, "
              "and `orbitrows undo` reverts the session's last change; send the data part as its own message after",
    "tool": "for the commands and their options, run `orbitrows --help` or `orbitrows <command> --help`",
    "unrelated": "the message is not about the store's data; nothing was run",
}


async def run(model, prompt: str) -> str | None:
    try:
        kind, probs = (await ask(model, {"prompt": prompt}, {"kind": choice(
            CONTEXT, "What does the message ask for?", KINDS, RULES)}))["kind"]
    except Exception:                                         # the model's call failed: the steps decide
        return None
    if kind not in SAY or probs.get(kind, 0) < SURE:          # data, or no answer of these
        return None
    return f"Not a database request: {SAY[kind]}."
