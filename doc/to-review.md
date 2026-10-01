# To review

Decisions taken to move forward, and assumptions to check later on real data. Each item says why it's open and what would settle it. Tick an item when it's decided, or delete it when it no longer applies.

## Store upload: column profile

- [ ] **Is the column type useful?** Jev classifies each column's type (`string`, `enum`, `integer`, `number`, `boolean`, `date`, `datetime`), stored in `store_column.schema`.
  - Planned use: generating the staging version at merge time.
  - If the staging step doesn't need it, drop the type question. It only takes a Choice out of the first Jev request (`TYPE_CRITERIA` in `src/pipeline/context.py`).
- [ ] **The merge needs more than `{"name", "type"}` in `schema`** (raised by the `poc` session). The PoC's conversion algorithms read:
  - `x-type`, `x-decimals` and `pattern` (parse_cell);
  - `x-unit`, `x-currency`, and `x-decimals` for rounding (units.py);
  - the list of values of each closed list (`enum`).

  Decide where these come from before building the merge: added to `schema` at upload, computed when the merge starts, or derived from the stored context (`header_context`, `context_category`).
- [ ] **`enum` isn't a Table Schema type.** Table Schema writes a closed list as `{"type": "string", "constraints": {"enum": [...]}}`.
  - If we validate with a Table Schema tool (such as frictionless `validate()`), convert `enum` to that form and fill the list of values.
- [ ] **Types and roles are judged from 5 sample rows** (`SAMPLE_ROWS`).
  - Risk: a column whose first rows are empty or misleading (a price column with whole numbers only, an `enum` whose repeats appear later) is typed mostly from its header.
  - Check on real stores, and raise the sample if needed.
- [ ] **Low-confidence context roles.** On the PoC cases, `Unité poids` came out `both` at 42% (expected `dedicated_context`), `weight_unit` at 45%, and `Weight unit (kg)` at 55%.
  - The links and categories were still right.
  - Check whether a role confidence threshold is needed.
- [ ] **Flags are deferred to the merge.** Nothing is flagged at upload. These cases must be raised when the merge uses the column:
  - an uncertain role (`context_classify = 4`);
  - the category `other` (`3`);
  - a `header_context` whose header didn't resolve (`header_context` is null);
  - a context column that describes nothing.
- [ ] **The conflict question** (two context columns claim one column) is the only question asked at upload. It was never seen in the PoC's 12 cases. Check that it stays rare.
- [ ] **header_context uses GLiNER2 + the dictionary** (`src/pipeline/units.py`), and it never guesses.
  - **Benchmark (207 headers):** 88% exact, **0 wrong values**, 4 kept ambiguous, 18 missed, 2 false positives.
  - **Setup it was measured with:** `fastino/gliner2-multi-v1`, threshold 0.5, gliner2 2.0.0, torch 2.14.0+cpu, transformers 4.57.6, pint 0.26.1, babel 2.18.0.
  - **The trade-off:** before the resolver review it scored 92% exact, but with 1 wrong value and several guessed currencies.
- [ ] **`header_context` can hold a currency symbol, not a code.** A symbol shared by several currencies (`$`: USD, CAD, AUD…; `¥`; `£`) is stored as written. The merge must resolve it from the store's currency setting, or ask the user.
- [ ] **Misses caused by GLiNER2's labels**, which the resolver now trusts:
  - `Thaman (MAD)`: "MAD" is labelled as a language;
  - `Poids en kg`: "en" is labelled as a language, which conflicts with kg.

  A misread label can make a header miss, but never gives a wrong value.
- [ ] **Remaining misses:**
  - currency words with no single Babel name (`DH`, `dirham(s)`, `dollars`, `dólares`);
  - spans GLiNER2 doesn't find (`Prix MAD`, `prix_mad`, `Contenance (cl)`);
  - false positives that Jev should filter out upstream (`Taille (L)`, `Pays (DE)`).
- [ ] **Relationship between declarations (experiment, `CLASSIFY_RELATION`; benchmarked, kept off).** The classifier answers "rate" almost always (15/15 compound headers, 5/6 independent pairs), and 5 simple headers regress. Keep it off unless a better relationship signal is found. When a header holds several declarations, GLiNER2 classifies the relationship: rate / multiply / independent / unclear. A rate becomes `EUR/100g` and a product `m ** 2`. The quantity label (`unit quantity`) is used only in this mode.
  - **Why this method:** GLiNER2 never extracts an operation word as an entity, and relation extraction and A/B pair classification failed. Classifying the header in plain language got 13/19 on the probe headers.
  - **Its known error:** `Prix (€) Poids (kg)` is classified as a rate (0.86) and would store **EUR/kg** for a plain price column, a wrong value. The same error appeared with every GLiNER2 method tried, and its confidence overlaps real rates, so no threshold can separate them.
  - **Also:** subject words like `Peso` or `Preis` can be read as part of a rate, which leads to a refusal (a miss, never a wrong value).
- [ ] **Ask the user to confirm an uncertain relationship** (decided, not built yet). When GLiNER2 reads a rate or a product, the CLI would show the reading ("Prix (€) Poids (kg): euros per kilo?") and store it only once confirmed. This is the answer to the error above. No Jev here, by decision.
- [ ] **The direction of a rate is taken from the roles** (a currency is the numerator), not read from the header. `kg/EUR` would become EUR/kg.
- [x] **A price per piece or pack is stored as a plain currency** (decided): `Prix à la pièce (€)` → EUR, `Price per pack of 12 (USD)` → USD. GLiNER2 doesn't extract counting units, so the basis isn't read. The currency is still correct.
- [x] **Area and cubic units** (fixed): CLDR area units are loaded (`mètres carrés` → `m ** 2`, `sq ft` → `ft ** 2`), and a span is read as a whole, never from one of its words.
- [ ] **`Alcool (% vol)` → nothing** (it was `%`). GLiNER2's span is `% vol` (percent by volume), which isn't readable as a whole, and reading `%` from it alone would be a partial reading. It's safe, but it's a loss. Fix it only through a general rule, for example percent qualifiers, never a special case.
- [ ] **`Taman (DH l kilo)` → kg** is a wrong value: GLiNER2 doesn't extract "DH", only "kilo".
- [ ] **Physical ratios** (e.g. `g/l`, `km/h`) are rejected: a compound must be a currency over a physical unit. Define their structure if a store needs them.
- [ ] **US vs imperial units** aren't distinguished. The dimension check can't tell a US gallon from an imperial one.
- [ ] **GLiNER2's cost:**
  - a 1.2 GB model, downloaded on the first run;
  - about 7 s to load, in the background while Jev answers;
  - about 200 ms per header, on CPU (PyTorch CPU build).
  
  The machine has an RTX 4060: switching to the CUDA build would speed it up, at the cost of a much larger install.
- [ ] **Enum detection.** Removed from the upload, and meant to be its own later step (the diagram puts it at merge time). The `enum` type now partly overlaps with it; decide whether it replaces that step.

## Prompt guardrail and commands (Jev intent)

- [ ] **Cut-off at 0.5** for "on topic", "about the store / incoming", and command detection. It's untuned, so tune it on real prompts.
- [ ] **Near-tie file targets resolve to the higher score** ("reverse the table": store 0.64 vs incoming 0.43). This was accepted; check that users don't find it surprising.
- [ ] **A request mixed with an off-topic one is refused as a whole** ("sort my store, then tell me the weather"). The alternative is to run the CSV part only.
- [ ] **"undo that" sits right on the cut-off** (on-topic at 0.50) and is currently caught as the undo command. Plain words like "export" or "help" aren't handled before Jev.
- [ ] **One command per prompt.** "merge them then show me the store" runs only the merge.

## Commands

- [ ] **`/undo` semantics:** to discuss. Today it deletes the latest edit (a hard delete with cascade), and you don't want a real delete.
  - Since the schema moved to one `edit` timeline per session, undo is a stack across both files. `/undo store` refuses when the latest change was on the incoming (and the reverse), and says to undo that one first.
  - `/reset` now removes every edit of the session, on both files.
- [ ] **`/merge`:** it checks its preconditions only; the merge pipeline isn't built.

## CLI

- [ ] **Each run starts a new session.** There's no way to resume a store uploaded in an earlier run.
- [ ] **A path containing spaces isn't recognised as a file** in the prompt line.
- [ ] **Running a prompt** (turning it into a MongoDB-style update and applying it) isn't built. Today the CLI only says which file a prompt targets.

## Database

- [ ] **An optional CHECK tying `edit.source = 1` (merge) to `incoming_id IS NOT NULL`.** It's not in the design yet; add it if you want the database to enforce it.
- [ ] **Column edits are now versioned** (`…_column_revision`), but nothing in the CLI writes them yet. The design says a rename re-runs the context detection for that column.
- [ ] **PostgreSQL vs SQLite:** we're staying on PostgreSQL for now. Reconsider if shop owners must install the CLI themselves, since SQLite needs no server. What switching would cost:
  - text positions;
  - partial unique indexes;
  - no GIN index;
  - foreign keys turned on per connection.
