# OrbitRows

A terminal app that reconciles a **store CSV export** with an **incoming update CSV**. It proposes changes and applies nothing until a human approves them.

- **Jev** (TypeSafe System One) handles the judgment calls: column meanings, column links, and ranking of ambiguous row candidates.
- **Deterministic code** handles everything that must be exact: key matching, fuzzy/embedding search, unit and currency conversion, and validation.
- **Textual** provides the review UI, with two human checkpoints: approving the column mapping, then approving the proposed updates.

The design comes from the approved PoC (`../poc`).

## Setup

```bash
uv sync
cp .env.example .env   # then fill in OPENROUTER_API_KEY and DATABASE_URL
uv run --env-file .env alembic upgrade head   # create/update the PostgreSQL schema (PG15+)
uv run --env-file .env orbitrows
```

## Using it

One prompt field. Type a CSV path, a request in plain words, or both in one line.

| Prompt | File | Store loaded | Result |
|---|---|---|---|
| yes | no | no | error: upload a store first |
| no | yes | no | the file becomes the store |
| yes | yes | no | the file becomes the store, the prompt applies to it |
| no | yes | yes | the file becomes the incoming |
| yes | yes | yes | the file becomes the incoming, Jev decides which file the prompt is for |
| yes | no | yes | Jev decides which file the prompt is for |

Jev first checks that every prompt is about the CSV files; anything else is refused. When both files are loaded, it decides whether the prompt changes the store, the incoming, or both.

Commands: `/preview` (20 rows, 6 columns), `/undo store`, `/undo incoming`, `/merge`, `/reset`. They can also be asked in words ("go back please", "start over", "merge them"): Jev recognises them in the same request as the checks above.

## Layout

```
src/                imported as `orbitrows`
  pipeline/         one module per stage: context, columns, rows, cells
  services/
    cli/            terminal UI
      app.py        Textual app and the `orbitrows` entry point
      screens.py    intro screen, prompt screen (routing + commands)
      crash.py      unexpected errors -> short message + log file
    csv/
      reader.py     CSV loading (delimiter sniffing, validation)
    db/
      models.py     PostgreSQL schema (see doc/database-structure.md)
      workspace.py  upload, latest version, undo, reset
    jev/
      client.py     Jev client (OpenRouter, jev-1.13)
      intent.py     is a prompt on topic, and for which file
migrations/         Alembic migrations (`alembic revision --autogenerate -m ...` after editing models.py)
tests/
```

## Tests

```bash
uv run --env-file .env pytest   # test_db.py is skipped without DATABASE_URL
```
