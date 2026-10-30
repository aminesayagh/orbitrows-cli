# Architecture

How OrbitRows turns a store's CSV files into tables, and a request in plain English into a checked SQL program. Version 0.1.0.

## The big picture

```
CSV files ──► store import ──► session.db (SQLite tables + their classification)
                                   │
"Raise the price of the            ▼
 black variants by 10%" ──► prompt pipeline ──► SQL program ──► session (run, undo, save)
```

Two kinds of work are kept apart:

- **Judgement calls go to Jev** (TypeSafe System One), asked as typed questions: a choice among options, or a yes/no. Each answer comes with probabilities.
- **Everything that must be exact is code:** reading numbers, units and dates, building SQL, checking it, running it.

**One rule runs through every step: never guess.** An answer below the sure threshold (`SURE = 0.7`, `src/pipeline/prompt/__init__.py`) becomes a question to the user. A request that can't be built completely is refused with its reason; it never runs with a restriction missing.

## The store import (`src/pipeline/store/`)

`orbitrows store` reads each CSV file, then classifies it phase by phase, asking Jev and stopping on a question when unsure:

| Phase | Module | Decides |
|---|---|---|
| header | `header.py` | whether the first row is a header |
| types | `types.py`, `profile.py` | each column's type, and its profile (empty cells, dates, values) |
| context | `context.py` | units and currencies a header declares ("Poids (kg)") |
| roles | `roles.py` | what each column is: a key, a measure, a label, a context column |
| keys | `keys.py` | the keys that identify a record |
| joins | `joins.py` | repeated records split into their own tables |
| schema | `schema.py` | the SQLite tables, built and proven: every file must be rebuilt exactly from them, or nothing is written |

The result is `session.db`: the store's tables plus the classification (`_store_tables`, `_store_columns`…). The column profile computed here is the only place where data values are read for the pipeline: after a message, only the executed SQL touches the rows.

## The prompt pipeline (`src/pipeline/prompt/`)

`pipeline.py` chains the steps. Each reads the previous steps' outputs and adds one answer.

| # | Step | Module | Input → output | Tools |
|---|---|---|---|---|
| 0 | Guard | `guard.py`, `source_route.py` | the message → "a request about the data" (else the right command), and which sources it reads | Jev |
| 1 | Requests | `request_graph.py`, `split.py`, `coref.py`, `link.py` | the prompt → its requests, in order, with how they relate: `THEN` (takes the other's rows or table state) or `AND` (reads the original snapshot); "them" resolved | GLiNER2, spaCy, Jev |
| 2 | Operation | `operation.py` | each request → show, change, delete or add | Jev |
| 3 | Values | `values.py` | the words → which are stored values ("Morocco" in suppliers' country), proven by SQL on the store | GLiNER2, meaning model, Jev |
| 4 | Sources | `sources.py` | each request → the table it works on, and the tables it reads | Jev |
| 5 | Parts | `columns.py`, `parts.py` | the columns the request names; what it shows, or what it changes | spaCy, meaning model, Jev |
| 6 | Conditions | `conditions.py` | which rows: one condition per column and written value | number reader, Duckling, Jev |
| 6b | Formula | `formula.py`, `src/pipeline/expressions/` | a change's calculation, as a graph of steps | catalog, Jev |
| 6c | Grouping | `grouping.py` | the conditions joined by AND / OR; sort, limit, groups | spaCy, Jev |
| 7 | Ledger + assembly | `ledger.py`, `assemble.py` | every written value accounted for → one checked SQL statement per request | code only |

### Step 6: conditions, in stages

1. **Stage 1, the columns.** Jev judges which columns choose the rows, among those the request names, plus the main table's id, the date columns when it writes a date, the columns whose unit a written number converts to ("200 grams" → the weight), and, when it writes more numbers than its named columns take, the main table's other number columns.
2. **Stage 2, one condition per written value:** its operator (=, <, between, is empty…), its operand, and whether it's negated. Numbers come from the shared number reader (`src/units/quantity.py`): digits, words, fractions, percentages, "no / none / nothing" as 0. Dates come from Duckling (`src/units/dates.py`), as days or whole periods.
3. **Later stages**, asked together in one Jev call:
   - **2b** computed values ("more than twice their cost");
   - **2c** linked records ("products with at least 3 variants");
   - **2d** conditions on groups;
   - **2n** each negated condition shown to Jev as built, to confirm the request really leaves those rows out;
   - **2x** when two columns' conditions took the same words, which one they belong to.

### The formula (`src/pipeline/expressions/`)

A change such as *"raise by 15%, then take 5 off, but for the black ones apply the 15% after removing the 5; round to the nearest 5; never above twice the cost"* is built in four moves:

1. **Pieces.** Pass 1 (`steps.py`) cuts the sentence into records with a role: a calculation, a branch, a rounding, a limit, a filter.
2. **Types.** Each piece is matched to a tested operation of the **catalog** (`src/pipeline/operations/`, `operations.db`), found by meaning, not keywords.
3. **Parameters.** Each operation declares what it needs, and the sentence fills it. A percentage slot only takes a number written as one: "by 20" is asked about.
4. **Graph.** Pass 2 (`links.py`) decides which result feeds which piece, then the graph becomes one SQL expression: `CASE`, `ROUND`, `MIN`, `MAX`, `WHERE`.

### Grouping

Jev answers, for each pair of conditions, "both" (AND) or "either" (OR); the tree that fits every answer is built (`tree_of`). An OR is checked against the sentence's grammar: a condition written wholly outside an "X or Y" is joined by AND ("the **September** sales with a discount above 5 or a quantity of at least 2").

### The ledger (`ledger.py`)

Before assembly, every number, date and stored value the request writes must be used by one of its parts, and one written value can serve only one condition. Otherwise the request is refused with the reason ("the number '200 grams' used by no part of the request"), never run without one of its restrictions.

### Assembly (`assemble.py`)

Code only: one AST statement per request (`src/sql/`), conditions on other tables lifted into `IN (SELECT …)` chains along the foreign keys, then validated on the store. The session runs the program.

## The session (`src/services/session/`)

One background process per store holds one open SQLite transaction on `session.db`:

- each change runs in its own **savepoint**, so `undo` rolls back the last one;
- every change is appended to `journal.jsonl`, so a crash loses nothing silently;
- everything is **committed** on `exit`, when the terminal closes, or after 30 idle minutes;
- at start, it **preloads** the models, the units registry and the unit of every column header, so the first message doesn't wait on them (`src/services/ready.py`).

Questions pause a message. Once answered, it runs again from the start, replaying Jev's recorded answers (`Recorded` in `src/services/model/client.py`), so nothing is paid twice.

## Local models and services

| What | Used for | Where |
|---|---|---|
| Jev (TypeSafe System One, paid) | every judgement call | `src/services/model/client.py` |
| GLiNER2 | spans: values, numbers, dates, header units | `src/services/extractor/client.py` |
| spaCy `en_core_web_sm` | grammar: mentions, lemmas, coordination | `src/pipeline/expressions/words.py` |
| paraphrase-multilingual-MiniLM | meaning: column names, values | `src/pipeline/prompt/values.py` |
| bge-small-en | catalog search | `src/pipeline/operations/index.py` |
| Duckling (Docker) | dates ("last month", "October 4") | `src/units/dates.py` |
| pint + the units dictionary | units and conversions | `src/units/` |

## Measured (2026-10-30)

| | Result |
|---|---|
| Regression sets (174 requests the pipeline supports) | 159 right; the 15 others are tracked issues |
| Hard sets (129 requests: two dates, two numbers, negations, nested AND / OR) | 115 right, 4 wrong, 10 refused with their reason |
| Fresh client-like set (24 requests written before any run) | 22 right on its first run |
| Time per message | about 4 s (local work about 2 s, Jev about 2 s); the first of a session about 8 s |

Wrong answers and refusals are tracked one per issue. The benchmark sets aren't part of the repository.

## Code layout

```
src/                    imported as `orbitrows`
  pipeline/
    store/              the store import, one module per phase
    prompt/             the prompt pipeline, one module per step
    expressions/        the formula: pass 1 (steps.py), pass 2 (links.py), words.py
    operations/         the catalog of tested operations (operations.db) and its builders
    merge/, incoming*.py  supplier files: classified, matched to the store by key
  services/
    cli/                the `orbitrows` command
    session/            the background session: transaction, savepoints, journal
    model/, extractor/  Jev and GLiNER2 clients
    storage/            the store registry, a store's folder, export
  units/                numbers (quantity_parser), units, dates (Duckling)
  sql/                  the SQL AST and its SQLite DDL / DML
tests/                  no paid model call: stubs and saved answers
doc/                    the full CLI reference
```
