# OrbitRows CLI: full reference

Every command, option, exit code, file and session behaviour in detail. The short guide is the [README](../README.md); how a message becomes SQL is in [ARCHITECTURE.md](../ARCHITECTURE.md).

## Setup

```bash
uv sync
cp .env.example .env   # then fill in TYPESAFE_API_KEY
uv run --env-file .env orbitrows setup   # once: everything a message needs (a few GB and minutes the first time)
uv run --env-file .env orbitrows --help
```

`orbitrows setup` prepares, and checks, each part a message needs, skipping what is already there:

- **Duckling**, the service dates are read with: started in Docker (container `orbitrows-duckling`, image
  `rasa/duckling`), unless one already answers at `DATEPHRASE_DUCKLING_URL` (default `127.0.0.1:8000`). Docker must be
  installed. Each session starts it again when it isn't running. Without it, requests that mention a date are refused;
  the others run.
- **The models**: GLiNER2, the meaning model (paraphrase-multilingual-MiniLM), the operations search model
  (bge-small-en) and spaCy's English model. Each session then loads them in the background as soon as it starts, so
  its first message doesn't wait on them.

The words that point to other words ("them", "the copies") are found with spaCy's grammar, and Jev decides what each
one points to. (CorPipe did this until 2026-10-27; it was replaced for its non-commercial license and its 8.6 GB.)

## Using it

```bash
orbitrows store products.csv variants.csv -n "Orbitrow shop"   # prints: Store st_7f3a91c2 (Orbitrow shop)
orbitrows store legacy.csv --encoding cp1252                 # when its encoding is already known
orbitrows answer q_1a2b3c4d REAL                              # when the import stopped on a question (exit 3)
orbitrows incoming supplier.csv                               # for the latest store
orbitrows message "raise all prices by 5%" --store st_7f3a91c2
orbitrows undo                                                # reverts the last change of the session
orbitrows exit                                                # saves the session's changes: prints Saved.
orbitrows export -o out/                                      # writes the store's files, rebuilt from its tables
```

| Command | Takes | Does |
|---|---|---|
| `store` | one or more CSV files, `-n/--name` and `--encoding` (optional) | checks every file, then imports it: Jev classifies the header, types, context, roles, keys and joined records; the store's tables are built and proven to give every file back. A non-UTF-8 file without a BOM pauses for an encoding choice before Jev runs. `--encoding` applies to all files when it is known. It writes the store folder, prints each table and every warning, and prints the store's id as the last line. When Jev is unsure, the import stops on its questions instead (below) |
| `answer` | a question's id and its answer (both optional), `-s/--store` | records an answer for a store import (`q_…`), incoming upload (`i_…`), or message (`m_…`). The paused work resumes with earlier model answers replayed |
| `incoming` | one CSV file, `-s/--store`, `--encoding` (optional) | profiles and classifies the file, asks about uncertain decisions, then saves a protected TEXT table and its incoming-to-store column matches in that store's `session.db`, through its session: unsaved until `exit` |
| `message` | the request (quoted or not), `-s/--store` | selects store and incoming sources first, then runs store-only requests through the session's SQL pipeline. Incoming sources are identified, but SQL generation using them is not implemented yet, so such requests stop without changing data. When Jev is unsure, it asks a question |
| `undo` | `-s/--store` | reverts the session's last change |
| `exit` | `-s/--store` | saves the session's changes and ends it |
| `export` | `-o/--output` folder (required), `--force`, `-s/--store` | rebuilds every uploaded file from the store's tables (the same rebuild the import's proof runs) and writes it into the folder: same file name, encoding, delimiter and line ending as the upload, the header row only if it had one, an empty cell for NULL, numbers as stored (`12.50` comes back `12.5`), rows in the order they were loaded. With a session open, it exports through it, so its unsaved changes are included. A file already in the folder refuses the export (nothing is written) unless `--force`. Prints one JSON object: `{"store", "name", "files": [{"file", "path", "rows"}]}` |

`--store` takes a store's id or name; without it, the latest ready store is used (for `answer`, the latest store waiting for answers, or left importing after its questions by a killed resume). Two `answer` commands on one store run one after the other: the second waits for the first. Progress goes to stderr and results to stdout; a refusal exits with 1, Ctrl+C with 130 (SIGTERM is handled like Ctrl+C).

Incoming row tables share `session.db` with the store. Each has triggers rejecting INSERT, UPDATE, and DELETE after its initial load; export still uses only store tables. The file is classified by the `incoming` command itself (it may pause on questions `i_…`), then written through the store's session (started if none runs), as one change of that session: like a message's, it is unsaved until `orbitrows exit` (stderr says so), reverted by `orbitrows undo`, and recorded in `_history` (`name` "Upload <file>", `prompt` NULL, the table's `create_table` AST, its `CREATE TABLE` and trigger statements); `_incoming_tables.created_by` references that row. The CSV copy waits in `incoming_pending/<upload>/` and moves to `incoming/<id>/` only when the session is saved: an undo removes it, and a crash leaves it there with the journal naming the lost upload. An earlier `incoming.db` is retired after the first successful upload in the new layout; its copied CSV files remain under `incoming/` for reimport, while its old classifications and matches are not carried over.

### Questions

An encoding is accepted automatically when the file has a UTF-8, UTF-16 or UTF-32 BOM, or reads as UTF-8 without a BOM. Other files pause at step `encoding`, before a model call. Each option shows a line decoded with that encoding; choose the one with the right letters using `orbitrows answer <id> <encoding>`. The chosen encoding is kept with the store and used again for export. The list includes common legacy encodings and charset-normalizer suggestions; the detector's ranking is not treated as proof. If you already know the encoding, use `store --encoding <name>`.

A Jev answer below its threshold is a question for the user: a choice (`options`) or a yes/no. The import runs phase by phase (header, types, context, roles, keys, joins) and stops after the first phase that has one. The store is then waiting for answers, `store` (or `answer`) exits with **3**, and stdout is one JSON object and nothing else, for the LLM that drives this CLI and asks the user:

```json
{"store": "st_7f3a91c2", "name": "Orbitrow shop", "step": "types",
 "questions": [{"id": "q_1a2b3c4d", "file": "products.csv", "step": "schema", "column": "price",
                "problem": "type uncertain: TEXT kept", "type": "choice",
                "options": [{"answer": "INTEGER", "probability": 0.5}, {"answer": "REAL", "probability": 0.4}]}],
 "notices": [{"file": "products.csv", "step": "schema", "column": "stock", "problem": "…", "probability": 0.7}]}
```

Answer each question with `orbitrows answer <id> <answer>`: one of its options' `answer` (any case), or `yes`/`no` for a `yes_no` question (it has no `options`). A wrong id or answer is refused with the valid ones. While questions are open, `answer` prints the same JSON with the remaining ones (exit 3). Notices are warnings without a question: each is shown once, in the JSON or in the final warnings, and the import goes on.

When the joins split repeated records out of a file into their own tables, the import stops once more (step `naming`) with one `name` question per table: it lists the table's columns and its generated name, `default` (`record_1`, `record_2`…). Answer with any name (made a SQL name; refused if another table has it) or `skip` to keep the generated one. The columns keep their headers' names, and the export gives every file back with its original headers either way.

If the import fails after its questions were asked (Jev down, Ctrl+C or SIGTERM, a crash), the store waits again with its answers kept: `orbitrows answer -s <id>` retries it. Before any question it's failed, and a store that would lose data is always failed.

Each store is a row in the registry, `~/.local/share/pilotstore/stores.db` (`$XDG_DATA_HOME` moves it): its id (`st_` + 8 hex), its optional name, and its status (`0` importing, `1` ready, `2` waiting for answers, `-1` failed). Each store also has its own folder, `stores/<id>/` next to the registry:

- `files/`: the uploaded CSV files, unchanged; the import (and every resume) reads these copies;
- `import.json`: the import's progress, written after each new answer (temporary file, then renamed): `files` (upload order), `encodings` (explicit or user-chosen readings), `calls` (every Jev answer with its probabilities, the user's answers recorded as certain), `names` (the table name chosen for each naming question), `step` and `open` (the current pause and its questions), `answered` (each question with the user's answer), `shown` (the notices already printed). It's kept once the import is done;
- `session.db`: the store's tables with their rows, plus the classification tables (`_store_tables`, `_store_columns`, `_column_context`, `_export`) and `_history`, the log of changes: its first row is the import, with the tables' `create_table` ASTs (`json_ast`) and the `CREATE TABLE` statements run (`sql`). `_store_tables.created_by` and `_store_columns.edited_by` reference the `_history` row of the request that created the table / last changed the column (the import, row 1, for now). It's written in one transaction, and only if every file can be rebuilt from it;
- `original.db`: a copy of `session.db` taken right after the import.

`foreign_keys` and `synchronous` are per-connection settings in SQLite: every connection that writes `session.db` must set them again.

### Sessions

A store's messages run in a **session**: a background process per store (`python -m orbitrows.services.session`, started by the first `message`) that keeps one SQLite transaction open on `session.db`. The CLI talks to it over `stores/<id>/session.sock`; its output goes to `session.log`.

- **When it's saved.** Each change (a message that changes rows or the schema, with its `_history` row) stays uncommitted: other connections don't see it. The session commits everything when it ends: `orbitrows exit` (prints `Saved.` once committed), the terminal that started it closing (only a real terminal: commands run without one, like an LLM's shell, never end it this way), `ORBITROWS_IDLE` seconds without a request (default 1800), or SIGTERM/SIGHUP.
- **Messages and their questions.** The session runs the prompt pipeline on its own connection, so it sees the session's unsaved changes; the models stay loaded between messages, and progress goes to `session.log`. A message ends in one of three ways:
  - **Questions** (exit 3): the same JSON as the import's, with ids `m_…` (`{"store", "name", "step", "questions": [{"id", "request", "step", "problem", "type", "options"?}], "notices"}`). Answer them with `orbitrows answer m_… <answer>` (by default, the latest ready store with an open session). When the last one is answered, the message runs again, replaying Jev's recorded answers. One message waits at a time: a new message replaces it, and says so in a notice.
  - **Applied** (exit 0): `{"store", "name", "applied": [{"request", "sql", "columns", "rows"} | {"request", "sql", "changed"}], "notices"}`.
  - **Refused** (exit 1): the pipeline found errors. Nothing is applied, and the errors are printed on stderr. A request that would change the table structure (add, drop or rename a column or a table: a `CREATE`, `ALTER` or `DROP` statement) is refused too, with "Changing the table structure isn't supported yet: nothing was changed.", so the export map and the classification stay true.

  Notices are the pipeline's warnings without a question (for example "changes no record" or "changes every record"). Each is shown once per message. A message waiting for answers lives only in the session's memory: it's lost when the session ends or its run fails.
- **Undo.** Each change runs in its own savepoint; `undo` rolls back to the last one, `_history` row included. A query, or a request that changes no row, is no change: there is nothing to undo for it. A statement SQLite refuses cancels the whole message.
- **Crash journal.** A crash loses the open session. So each change is also appended (and synced to disk) to `journal.jsonl` as `{"n", "prompt", "sql", "at"}`, an undo as `{"undo": n}`; a successful commit removes it. The next session finds it, keeps it as `journal-lost-<time>.jsonl`, and the next command prints the lost changes (each prompt and its SQL) on stderr. They're not replayed: send them again if they're still wanted.

## Layout

```
src/                imported as `orbitrows`
  pipeline/
    store/          the store import, one module per step: header, types, profile, context, roles, keys, joins, schema
      run.py        the steps chained: classify (the model's questions), build (the schema and the round-trip proof)
  services/
    cli/
      main.py       the `orbitrows` command: store, answer, incoming, message, undo, exit, export
      crash.py      unexpected errors -> short message + log file
    session/
      server.py     a store's session: one open transaction, a savepoint per change, COMMIT when it ends
      client.py     the CLI's side: one request over the socket, starting the server when none runs
    csv/
      reader.py     CSV reading (encoding and delimiter sniffing)
    model/
      client.py     the model behind every step: Jev (TypeSafe System One) by default, MODEL picks another
    extractor/
      client.py     the span extractor: GLiNER2 (local) by default
    storage/
      registry.py   stores.db: the list of imported stores (CLI plan §3.3)
      store.py      a store's folder: files/, import.json, session.db, original.db
      export.py     the store's files rebuilt from session.db (orbitrows export)
  units/            header_context: GLiNER2 finds the unit in a header, the dictionary normalises it ("Poids (kilos)" -> kg)
  sql/
    ddl.py          the create_table AST node and the SQLite DDL it is checked with
tests/
```

## Tests

No test calls a model: they use stubs and the PoC cases in `tests/fixtures/cases/`.
`tests/test_hard_csv_workflows.py` also runs 20 representative CSV byte cases through the CLI, all import phases,
SQLite and export, plus a related-file import and an import → message → undo → exit flow. External model judgments
are fixed in those tests; the suite checks workflow behavior and data preservation, not live model accuracy.

```bash
uv run python -m pytest
```
