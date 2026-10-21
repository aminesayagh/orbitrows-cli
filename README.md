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
orbitrows answer q_1a2b3c4d REAL                              # when the import stopped on a question (exit 3)
orbitrows incoming supplier.csv                               # for the latest store
orbitrows message "raise all prices by 5%" --store st_7f3a91c2
```

| Command | Takes | Does |
|---|---|---|
| `store` | one or more CSV files, `-n/--name` (optional) | checks every file (one bad file, or two files with the same name, refuses the store), registers the store, and imports it: Jev classifies the header, types, context, roles, keys and joined records; the store's tables are built and proven to give every file back. It writes the store folder, prints each table and every warning, and prints the store's id as the last line. When Jev is unsure, the import stops on its questions instead (below) |
| `answer` | a question's id and its answer (both optional), `-s/--store` | records the answer to one open question of a store waiting for answers. Once none is open, the import runs again from the start, replaying every answer already given (no new Jev call for them), and ends like `store`, or stops on the next questions. With no question and none open, it only retries the import (after a failure) |
| `incoming` | one CSV file, `-s/--store` | checks the file for a store; not saved yet |
| `message` | the request (quoted or not), `-s/--store` | finds the store; running requests isn't built yet |

`--store` takes a store's id or name; without it, the latest ready store is used (for `answer`, the latest store waiting for answers). Progress goes to stderr and results to stdout; a refusal exits with 1, Ctrl+C with 130.

### Questions

A Jev answer below its threshold is a question for the user: a choice (`options`) or a yes/no. The import runs phase by phase (header, types, context, roles, keys, joins) and stops after the first phase that has one. The store is then waiting for answers, `store` (or `answer`) exits with **3**, and stdout is one JSON object and nothing else, for the LLM that drives this CLI and asks the user:

```json
{"store": "st_7f3a91c2", "name": "Orbitrow shop", "step": "types",
 "questions": [{"id": "q_1a2b3c4d", "file": "products.csv", "step": "schema", "column": "price",
                "problem": "type uncertain: TEXT kept", "type": "choice",
                "options": [{"answer": "INTEGER", "probability": 0.5}, {"answer": "REAL", "probability": 0.4}]}],
 "notices": [{"file": "products.csv", "step": "schema", "column": "stock", "problem": "…", "probability": 0.7}]}
```

Answer each question with `orbitrows answer <id> <answer>`: one of its options' `answer` (any case), or `yes`/`no` for a `yes_no` question (it has no `options`). A wrong id or answer is refused with the valid ones. While questions are open, `answer` prints the same JSON with the remaining ones (exit 3). Notices are warnings without a question: each is shown once, in the JSON or in the final warnings, and the import goes on.

If the import fails after its questions were asked (Jev down, Ctrl+C, a crash), the store waits again with its answers kept: `orbitrows answer -s <id>` retries it. Before any question it's failed, and a store that would lose data is always failed.

Each store is a row in the registry, `~/.local/share/pilotstore/stores.db` (`$XDG_DATA_HOME` moves it): its id (`st_` + 8 hex), its optional name, and its status (`0` importing, `1` ready, `2` waiting for answers, `-1` failed). Each store also has its own folder, `stores/<id>/` next to the registry:

- `files/`: the uploaded CSV files, unchanged; the import (and every resume) reads these copies;
- `import.json`: the import's progress, written after each new Jev answer (temporary file, then renamed): `files` (upload order), `calls` (every Jev answer with its probabilities, the user's answers recorded as certain), `step` and `open` (the current pause and its questions), `answered` (each question with the user's answer), `shown` (the notices already printed). It's kept once the import is done;
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
      main.py       the `orbitrows` command: store, answer, incoming, message
      crash.py      unexpected errors -> short message + log file
    csv/
      reader.py     CSV reading (encoding and delimiter sniffing)
    model/
      client.py     the model behind every step: Jev (TypeSafe System One) by default, MODEL picks another
    extractor/
      client.py     the span extractor: GLiNER2 (local) by default
    storage/
      registry.py   stores.db: the list of imported stores (CLI plan §3.3)
      store.py      a store's folder: files/, import.json, session.db, original.db
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
