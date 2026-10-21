# OrbitRows

A command-line tool that reconciles a **store CSV export** with an **incoming update CSV**. It proposes changes and applies nothing until a human approves them.

- **Jev** (TypeSafe System One) handles the judgment calls: column meanings, column links, and ranking of ambiguous row candidates.
- **Deterministic code** handles everything that must be exact: key matching, fuzzy/embedding search, unit and currency conversion, and validation.
- **Three commands**, one process each, so a person or an agent (Claude, Codex) can drive it from a shell.

The design comes from the approved PoC (`../poc`).

## Setup

```bash
uv sync
cp .env.example .env   # then fill in OPENROUTER_API_KEY
uv run --env-file .env orbitrows --help
```

## Using it

```bash
orbitrows store products.csv variants.csv -n "Orbitrow shop"   # prints: Store st_7f3a91c2 (Orbitrow shop)
orbitrows incoming supplier.csv                               # for the latest store
orbitrows message "raise all prices by 5%" --store st_7f3a91c2
```

| Command | Takes | Does |
|---|---|---|
| `store` | one or more CSV files, `-n/--name` (optional) | reads every file (one bad file refuses the store), profiles each one with Jev, registers the store, and prints its id as the last line |
| `incoming` | one CSV file, `-s/--store` | checks the file for a store; not saved yet |
| `message` | the request (quoted or not), `-s/--store` | finds the store; running requests isn't built yet |

`--store` takes a store's id or name; without it, the latest ready store is used. Progress goes to stderr and results to stdout; a refusal exits with 1.

Each store is a row in the registry, `~/.local/share/pilotstore/stores.db` (`$XDG_DATA_HOME` moves it): its id (`st_` + 8 hex), its optional name, and its status (`0` importing, `1` ready, `-1` failed). The CSV files themselves aren't saved yet.

## Layout

```
src/                imported as `orbitrows`
  pipeline/         one module per stage: context, columns, rows, cells
    context.py      Jev column profile on upload: type, context role, category, which columns it describes
    units.py        header_context: GLiNER2 finds the unit in a header, the dictionary normalises it ("Poids (kilos)" -> kg)
  services/
    cli/
      main.py       the `orbitrows` command: store, incoming, message
      crash.py      unexpected errors -> short message + log file
    csv/
      reader.py     CSV loading (delimiter sniffing, validation)
    storage/
      registry.py   stores.db: the list of imported stores (CLI plan §3.3)
    jev/
      client.py     Jev client (OpenRouter, jev-1.13)
tests/
```

## Tests

```bash
uv run --env-file .env pytest
```
