"""Jev checks on a user prompt: can the CSV files answer it, and which file is it about?"""

from dataclasses import dataclass

from typesafe_sdk import AsyncTypeSafeClient, Choice, Noul

# ponytail: 0.5 = more likely yes than no; tune on real prompts once we have some
THRESHOLD = 0.5

# Jev sees the file names and column headers, not the rows: enough to tell whether the files can answer.
ON_TOPIC = Noul(
    instructions=(
        "Can `message` be answered or carried out using only the CSV files described in `files`? "
        "This covers reading them (viewing, counting rows, searching, filtering, questions about their "
        "contents or columns), reorganising them (sorting, reordering, reversing), and changing them "
        "(editing, cleaning, adding, deleting or merging rows, columns or values)."
    ),
    criteria={
        "true": (
            "The request is about these files' data or structure, even if it is vague or uses other words or "
            "another language than the column names (for example 'price' or 'cost' for a column 'prix_ht'). "
            "Judge only the request part: ignore greetings, jokes, thanks or small talk around it. If the message "
            "contains at least one request these files can serve, answer true."
        ),
        "false": (
            "It needs information these files do not hold, judging by their columns (for example personal "
            "facts, the weather, general knowledge), or it is small talk unrelated to the files."
        ),
    },
)

# One yes/no per file, not a single Choice: a prompt can be about both files at once.
TARGETS = {
    "store": Noul(
        instructions=(
            "Is `message` about the store file (`files.store`), the shop's own catalog export: "
            "reading it, reorganising it or changing it? The user may call it 'my products', 'my catalog', "
            "'my shop' or 'our stock'. A message can be about both files."
        ),
        criteria={"true": "The request reads or changes the store catalog.", "false": (
                "The store catalog is not involved. Products described as someone else's ('their products', "
                "'the supplier's products') are in the supplier file, not the store."
            )},
    ),
    "incoming": Noul(
        instructions=(
            "Is `message` about the incoming file (`files.incoming`), the supplier's file that will be merged "
            "into the store: reading it, reorganising it or changing it? The user may call it 'the supplier file', "
            "'their products', 'what they sent' or 'the new file'. A message can be about both files."
        ),
        criteria={"true": "The request reads or changes the supplier file.", "false": "The supplier file is not involved."},
    ),
}


# The app's commands, asked by prompt ("go back please" = /undo). One Choice: a prompt asks for one command at most.
COMMAND = Choice(
    instructions=(
        "Is `message` asking the app to run one of its commands, rather than asking about or editing the data? "
        "Pick the command it asks for, or none."
    ),
    criteria={
        "preview": (
            "Show the current store as it is, with no filter, sort or question about its contents "
            "(for example 'show me the store', 'let me see my file', 'aperçu')."
        ),
        "undo": (
            "Cancel the last change made to the files, one step back "
            "(for example 'go back please', 'undo that', 'annule')."
        ),
        "reset": (
            "Throw away every change and return to the store as it was first uploaded "
            "(for example 'start over', 'restore the original file')."
        ),
        "merge": "Merge the supplier file into the store (for example 'merge them', 'apply the supplier update').",
        "none": (
            "Anything else: questions about the data, filtering, sorting, editing values, "
            "or messages unrelated to the files, even when they use similar words about something else "
            "(for example 'go back to the beach', 'start over my diet', 'merge my two bank accounts')."
        ),
    },
)


@dataclass(frozen=True)
class Intent:
    on_topic: bool
    store: bool
    incoming: bool
    command: str | None = None  # "preview" | "undo" | "reset" | "merge"


async def classify(client: AsyncTypeSafeClient, message: str, files: dict[str, dict]) -> Intent:
    """`files` maps "store"/"incoming" to {"name": ..., "columns": [...]}; only the loaded ones.

    With a single file loaded, a prompt can only target that file, so the target questions are skipped.
    """
    questions = {"on_topic": ON_TOPIC, "command": COMMAND}
    if len(files) > 1:
        questions |= TARGETS  # independent of on_topic, so asked in the same request
    answer = await client.system_one(state={"message": message, "files": files}, questions=questions)
    yes = {name: a.noul >= THRESHOLD for name, a in answer.nouls.items()}
    if len(files) == 1:
        yes[next(iter(files))] = True
    command = answer.choices["command"]
    picked = command.choice if command.choice != "none" and command.probabilities[command.choice] >= THRESHOLD else None
    return Intent(yes["on_topic"], yes.get("store", False), yes.get("incoming", False), picked)
