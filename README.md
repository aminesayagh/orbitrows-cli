# OrbitRows CLI

Talk to your store's data in plain English. Import your store's CSV files once, then send requests such as *"Show the October sales of the Casa products"* or *"Raise the price of the black variants by 10%"*. OrbitRows turns each request into SQL, runs it, and shows you what changed. You can undo a change until you save.

When it isn't sure what you meant, it asks you a question instead of guessing. When a request can't be built safely, it refuses and says why: it never runs a request with part of it missing.

How a request becomes SQL is explained in [ARCHITECTURE.md](ARCHITECTURE.md). Every option and file is in the [full reference](doc/cli-reference.md).

## Requirements

- **Python 3.13** and **[uv](https://docs.astral.sh/uv/)**.
- **Docker**, for the date reader (Duckling). Without it, requests that mention a date are refused; the others work.
- **A TypeSafe API key** (`TYPESAFE_API_KEY`), for Jev, the model that makes the judgement calls.
- About **3 GB of disk** for the local models, downloaded once.

## Install

```bash
git clone https://github.com/aminesayagh/orbitrows-cli.git
cd orbitrows-cli
uv sync
cp .env.example .env          # then put your TYPESAFE_API_KEY in it
uv run --env-file .env orbitrows setup
```

`setup` starts Duckling in Docker and downloads the models. It's needed once, and it says plainly what isn't ready.

In the examples below, `orbitrows` stands for `uv run --env-file .env orbitrows`. A shell alias saves typing:

```bash
alias orbitrows='uv run --env-file .env orbitrows'
```

## Quick start

```bash
# 1. Import your store's files (once). Prints the store's id, e.g. st_7f3a91c2.
orbitrows store products.csv variants.csv sales.csv -n "My shop"

# 2. Ask about your data, or change it.
orbitrows message "Show the variants that cost more than 100"
orbitrows message "Raise the price of the black variants by 10%"

# 3. Not what you wanted? Undo the last change.
orbitrows undo

# 4. Save your changes and close the session.
orbitrows exit

# 5. Get your files back, with the changes, in their original format.
orbitrows export -o out/
```

## Commands

| Command | What it does |
|---|---|
| `setup` | Prepares Duckling and the models (once). |
| `store FILES… -n NAME` | Imports a store from its CSV files and prints its id. |
| `message "REQUEST"` | Runs a request in plain English: shows the rows, or changes them. |
| `answer ID ANSWER` | Answers a question the CLI asked, then carries on. |
| `undo` | Reverts the session's last change. |
| `status` | Shows the unsaved changes and the questions waiting for your answer. |
| `exit` | Saves the session's changes and ends it. |
| `export -o FOLDER` | Writes the store's files back, with the changes. |
| `incoming FILE` | Adds a supplier file to the store, to compare with or to update the store from. The file itself is never changed. |

Each command works on the latest store by default; `-s/--store` picks another, by id or name.

## When it asks a question

If a decision isn't sure enough, the command stops with **exit code 3** and prints the questions as JSON, each with an id and its options:

```bash
orbitrows answer m_1a2b3c4d yes
```

When the last question is answered, the request runs. Exit codes: **0** done, **1** refused (nothing changed, the reason is on stderr), **3** waiting for your answers.

## Saving and undoing

Your changes run inside a **session**, a background process per store:

- Each change applies at once, so the next request sees it, but nothing is saved yet.
- `undo` reverts the last change, as many times as you need.
- `exit` saves everything. The session also saves when the terminal that started it closes.
- **Nothing is saved without you.** After 30 minutes without a request, the session ends **without saving**. A crash or a reboot does the same. Your unsaved changes and any question waiting for your answer are kept on disk, and the next command restores them: undo still works, and `exit` saves them.
- `status` shows where things stand: the unsaved changes and the questions waiting. Run it first when you come back after a break.

## Use it from an AI agent

OrbitRows is built to be driven by an agent such as Claude Code or Codex: results are JSON, exit codes say what happened (0 done, 1 refused, 3 waiting for answers), and questions come with ids and options. The skill in [`skills/orbitrows/SKILL.md`](skills/orbitrows/SKILL.md) (the open Agent Skills format) teaches the agent the rules: check `status` after a resume, pass your words as they are, relay every question to you, and save only when you say so.

Install it by copying the folder into the agent's skills folder:

```bash
cp -r skills/orbitrows ~/.claude/skills/        # Claude Code, for every project
cp -r skills/orbitrows .agents/skills/          # Codex and other agents, in a project
```

## Good to know (version 0.1.0)

- **Write the value, and name the column when it's ambiguous.** "Quantity 0 and reserved 0" is safer than "nothing in stock and nothing reserved".
- **Write a percentage as one:** "by 20%". A plain "by 20" is asked about.
- Changes that add, drop or rename a column or a table aren't supported yet.
- A message takes about **4 seconds**, the first one of a session about 8.
- Known limits and planned work are tracked in the [issues](https://github.com/aminesayagh/orbitrows-cli/issues).

## Data and privacy

Your store lives on your machine, in `~/.local/share/pilotstore/`. What is sent to Jev to decide what you meant: your requests, the store's structure (table and column names, some values), and at import a few sample rows of each file. Your tables are never uploaded whole.

## Tests

```bash
uv run python -m pytest -q
```

No test calls a paid model: they use stubs and saved answers.
