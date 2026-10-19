# OrbitRows

A terminal app that reconciles a **store CSV export** with an **incoming update CSV**. It proposes changes and applies nothing until a human approves them.

- **Jev** (TypeSafe System One) handles the judgment calls: column meanings, column links, and ranking of ambiguous row candidates.
- **Deterministic code** handles everything that must be exact: key matching, fuzzy/embedding search, unit and currency conversion, and validation.
- **Textual** provides the review UI, with two human checkpoints: approving the column mapping, then approving the proposed updates.

The design comes from the approved PoC (`../poc`).

## Setup

```bash
uv sync
cp .env.example .env   # then fill in OPENROUTER_API_KEY
uv run --env-file .env orbitrows
```

## Using it

One prompt field. Type CSV paths, a request in plain words, or both in one line. The first message must include the store's CSV files.

| Prompt | Files | Store loaded | Result |
|---|---|---|---|
| yes | no | no | error: the first message must bring the store's CSV files |
| either | one or more | no | the files become the store (one store, registered in `stores.db`); a prompt then applies to it |
| either | one | yes | the file becomes the incoming; a prompt then goes to Jev, which decides which file it is for |
| either | several | yes | error: one incoming file at a time |
| yes | no | yes | Jev decides which file the prompt is for |

Each new store gets a row in the registry, `~/.local/share/pilotstore/stores.db` (`$XDG_DATA_HOME` moves it): its id (`st_` + 8 hex), its name (the first file's name for now), and its status (`0` importing, `1` ready, `-1` failed). The CSV files themselves aren't saved yet.

Jev first checks that every prompt is about the CSV files; anything else is refused. When both files are loaded, it decides whether the prompt changes the store, the incoming, or both.

Apart from the registry, files live in memory for the session. Commands: `/preview`, `/undo store`, `/undo incoming`, `/merge`, `/reset` (all placeholders until storage is designed). They can also be asked in words ("go back please", "start over", "merge them"): Jev recognises them in the same request as the checks above.

## Layout

```
src/                imported as `orbitrows`
  pipeline/         one module per stage: context, columns, rows, cells
    context.py      Jev column profile on upload: type, context role, category, which columns it describes
    units.py        header_context: GLiNER2 finds the unit in a header, the dictionary normalises it ("Poids (kilos)" -> kg)
  services/
    cli/            terminal UI
      app.py        Textual app and the `orbitrows` entry point
      screens.py    intro screen, prompt screen (routing + commands)
      crash.py      unexpected errors -> short message + log file
    csv/
      reader.py     CSV loading (delimiter sniffing, validation)
    storage/
      registry.py   stores.db: the list of imported stores (CLI plan §3.3)
    jev/
      client.py     Jev client (OpenRouter, jev-1.13)
      intent.py     is a prompt on topic, and for which file
tests/
```

## Tests

```bash
uv run --env-file .env pytest
```
