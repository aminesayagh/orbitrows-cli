# OrbitRows

A command-line tool that reconciles a **store CSV export** with an **incoming update CSV**. It proposes changes and applies nothing until a human approves them.

- **Jev** (TypeSafe System One) handles the judgment calls: column meanings, column links, and ranking of ambiguous row candidates.
- **Deterministic code** handles everything that must be exact: key matching, fuzzy/embedding search, unit and currency conversion, and validation.
- **Three commands**, one process each, so a person or an agent (Claude, Codex) can drive it from a shell.

The design comes from the approved PoC (`../poc`).

## Setup

```bash
uv sync
cp .env.example .env   # then fill in TYPESAFE_API_KEY
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
| `store` | one or more CSV files, `-n/--name` (optional) | checks every file (one bad file, or two files with the same name, refuses the store), registers the store, and imports it: Jev classifies the header, types, context, roles, keys and joined records; the store's tables are built and proven to give every file back. It writes the store folder, prints each table and every warning, and prints the store's id as the last line |
| `incoming` | one CSV file, `-s/--store` | checks the file for a store; not saved yet |
| `message` | the request (quoted or not), `-s/--store` | finds the store; running requests isn't built yet |

`--store` takes a store's id or name; without it, the latest ready store is used. Progress goes to stderr and results to stdout; a refusal exits with 1.

Each store is a row in the registry, `~/.local/share/pilotstore/stores.db` (`$XDG_DATA_HOME` moves it): its id (`st_` + 8 hex), its optional name, and its status (`0` importing, `1` ready, `-1` failed). Each store also has its own folder, `stores/<id>/` next to the registry:

- `files/`: the uploaded CSV files, unchanged;
- `session.db`: the store's tables with their rows, plus the classification tables (`_store_tables`, `_store_columns`, `_column_context`, `_export`) and `_history`, the log of changes: its first row is the import, with the tables' `create_table` ASTs (`json_ast`) and the `CREATE TABLE` statements run (`sql`). It's written in one transaction, and only if every file can be rebuilt from it;
- `original.db`: a copy of `session.db` taken right after the import.

`foreign_keys` and `synchronous` are per-connection settings in SQLite: every connection that writes `session.db` must set them again.

## Layout

```
src/                imported as `orbitrows`
  pipeline/
    store/          the store import, one module per step: header, types, profile, context, roles, keys, joins, schema
      run.py        the steps chained: classify (the model's questions), build (the schema and the round-trip proof)
  services/
    cli/
      main.py       the `orbitrows` command: store, incoming, message
      crash.py      unexpected errors -> short message + log file
    csv/
      reader.py     CSV reading (encoding and delimiter sniffing)
    model/
      client.py     the model behind every step: Jev (TypeSafe System One) by default, MODEL picks another
    extractor/
      client.py     the span extractor: GLiNER2 (local) by default
    storage/
      registry.py   stores.db: the list of imported stores (CLI plan §3.3)
      store.py      a store's folder: files/, session.db, original.db
  units/            header_context: GLiNER2 finds the unit in a header, the dictionary normalises it ("Poids (kilos)" -> kg)
  sql/
    ddl.py          the create_table AST node and the SQLite DDL it is checked with
tests/
```

## Tests

No test calls a model: they use stubs and the PoC cases in `tests/fixtures/cases/`.

```bash
uv run python -m pytest
```
