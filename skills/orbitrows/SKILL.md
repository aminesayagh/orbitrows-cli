---
name: orbitrows
description: Reads and changes a store's data (tables imported from CSV files) from plain-English requests, through the orbitrows CLI, which turns each request into checked SQL, asks when unsure, and keeps every change undoable until it is saved. Use when the user wants to import a store's CSV files, ask about or change its records (show, count, add, update, delete rows), compare it with a supplier's file, undo or save changes, or export the files back.
license: Proprietary
compatibility: Requires the orbitrows CLI (Python 3.13, uv), Docker for reading dates, and a TYPESAFE_API_KEY in the environment.
metadata:
  author: aminesayagh
  version: "0.1.0"
---

# OrbitRows: a store's data in plain English

The `orbitrows` CLI turns the user's request into SQL and runs it on their store. **You never write SQL and never touch the database files yourself**: you pass the user's words to the CLI, answer its questions when the user has already settled them (else ask the user), and report what it did. Its safety (it asks instead of guessing, refuses what it can't build, lets every change be undone) only holds if you follow the rules below.

If `orbitrows` isn't installed, tell the user: `uv tool install git+https://github.com/aminesayagh/orbitrows-cli`, their key in `~/.config/orbitrows/env` (`TYPESAFE_API_KEY=…`), then `orbitrows setup`.

## Rule 1: after any start or resume, check the status first

```bash
orbitrows status
```

It prints one JSON object and never starts anything:

```json
{"store": "st_7f3a91c2", "name": "My shop", "session": "closed",
 "unsaved": [{"n": 1, "prompt": "Archive the draft Casa products created in September"}],
 "waiting": {"prompt": "Raise the price of the Orbit Hoodie variants by 20",
             "questions": [{"id": "m_1a2b3c4d", "problem": "'20' is not written as a percentage: 20% or an amount of 20?"}]}}
```

Tell the user plainly: which changes are **not saved yet**, and which question is **still waiting for their answer**. Unsaved changes survive an interruption (the next command restores them); they are saved only by `orbitrows exit`.

## Rule 2: send the user's request as they wrote it

```bash
orbitrows message "Show the October sales of the Casa products"
```

- Pass the user's words. Don't translate them into SQL or into your own phrasing; you may add a clarification the user gave you.
- One message may hold several requests ("archive the draft products, then show them").
- `-s <store id or name>` picks a store; without it, the latest store is used.

The exit code tells you what happened:

| Exit | Meaning | What you do |
|---|---|---|
| **0** | Done | Report the result (below). |
| **3** | Waiting for answers | Rule 3. |
| **1** | Refused; the reason is on stderr | Rule 4. |

On exit 0, stdout is `{"store", "name", "applied": [...], "notices": [...]}`. Each `applied` entry is a read (`"columns"`, `"rows"`; `"more": true` when there were over 1000 rows) or a change (`"changed"`: the number of rows). Show reads as a short table. For a change, say how many rows changed, and that it is **not saved yet**: `undo` reverts it, `exit` saves it. Pass on each `notices` entry that matters to the user (for example "changes every record").

## Rule 3: answer what the user has settled, ask the user the rest

On exit 3, stdout lists the open questions, with a `to_answer` guide:

```json
{"store": "st_7f3a91c2", "name": "My shop", "step": "operation",
 "questions": [{"id": "m_1a2b3c4d", "request": "r1", "step": "formula",
                "problem": "'20' is not written as a percentage: 20% or an amount of 20?",
                "type": "choice", "options": [{"answer": "20%", "probability": 0.55}, {"answer": "an amount of 20", "probability": 0.45}]}],
 "notices": [],
 "to_answer": {"who": "you, when the user's own words or the store's data settle it (add --by agent); otherwise ask the user. …",
               "command": "orbitrows answer <id> <answer>",
               "formats": {"choice": "one of its options' answer values", "yes_no": "yes or no",
                           "formula": "an expression in x, e.g. x * 10", "name": "a table name, or skip"}}}
```

**`to_answer.who` says who answers**, from the user's setting (`ORBITROWS_ANSWERS` in `~/.config/orbitrows/env`):

- **`agent` (the default):** answer a question yourself **only when it is already settled**: the user said it in this conversation ("raise by 20 percent", "the prices are in euros"), or the store's data shows it. Add `--by agent`, then **tell the user what you answered and why**, so they can correct it (`undo` reverts the change):

```bash
orbitrows answer m_1a2b3c4d "20%" --by agent
```

  When nothing settles it, ask the user, with the options, and wait. **Never guess**, and never take the likeliest option because it is likeliest: the question exists because the tool couldn't decide.
- **`user`:** show every question to the user, with its options, and answer with their choice (no `--by`).

The answer is the option's `answer` value for a choice, `yes` / `no` for a yes/no, an expression in `x` for a formula (a unit conversion: `x * 10`), a name or `skip` for a table's name. A wrong id or answer is refused with the valid ones listed. When the last question is answered, the request runs, and you are back at Rule 2's exit codes. When the reply has `answered_by_agent`, those are the answers you gave: they must already be known to the user. A new `message` replaces a message still waiting for answers.

## Rule 4: a refusal is an answer, not an obstacle

On exit 1, nothing was changed. Tell the user the reason in plain words, for example "the number '200 grams' is used by no part of the request". Don't rephrase the request on your own until it passes; ask the user how to say it. What usually helps:

- **Name the column when two values could be confused:** "quantity 0 and reserved 0" rather than "nothing in stock and nothing reserved".
- **Write a percentage as one:** "by 20%"; "by 20" is an amount.
- **Write values as they are stored:** "the Casa brand", "status draft".

Changes to the table structure (adding, renaming or removing a column or a table) are refused for now: say so.

## Rule 5: saving is the user's decision

```bash
orbitrows undo     # reverts the last change; run it again to go further back
orbitrows exit     # saves every unsaved change and ends the session
```

- **Run `exit` only when the user asks to save.** Never as a clean-up step, and never at the end of a task on your own.
- An idle session (30 minutes), a crash or a reboot saves nothing: the changes come back with the next command, which says so on stderr.

## Other commands

| Command | Use |
|---|---|
| `orbitrows setup` | Once per machine: starts Duckling (dates) in Docker and downloads the models (a few GB, the first time only). It says what isn't ready. |
| `orbitrows store FILE.csv … -n "Name"` | Imports a store from its CSV files and prints its id. It may stop on questions (exit 3): same as Rule 3, with ids `q_…`. |
| `orbitrows incoming FILE.csv` | Adds a supplier's file to the store, to compare with or to update the store from (questions `i_…`). |
| `orbitrows export -o FOLDER` | Writes the store's files back, with the unsaved changes, in their original format. `--force` replaces files already there. |

## Things to avoid

- Writing or running SQL yourself, or opening `session.db`: the CLI's checks and undo would be bypassed.
- Answering a question the user hasn't settled, or without telling them; running `exit` without being asked.
- Sending the same refused request again in other words without the user.
- Hiding a notice or a refusal: the user must know what ran, what didn't, and why.
