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
uv run orbitrows
```

## Layout

```
src/                imported as `orbitrows`
  pipeline/         one module per stage: context, columns, rows, cells
  services/
    cli/            terminal UI
      app.py        Textual app and the `orbitrows` entry point
      screens.py    intro screen, source CSV prompt
      crash.py      unexpected errors -> short message + log file
    csv/
      reader.py     CSV loading (delimiter sniffing, validation)
    jev/
      client.py     Jev client (OpenRouter, jev-1.13)
tests/
```

## Tests

```bash
uv run pytest
```
