# To review

Since the first release (v0.1.0, 2026-10-30), each open item below is also a GitHub issue: https://github.com/aminesayagh/orbitrows-cli/issues

Decisions taken to move forward, and assumptions to check later on real data. Each item says why it's open and what would settle it. Tick an item when it's decided, or delete it when it no longer applies.

## Store upload: column profile

- [ ] **Is the column type useful?** Jev classifies each column's type (`string`, `enum`, `integer`, `number`, `boolean`, `date`, `datetime`), kept in the column profile (`ColumnProfile.type`).
  - Planned use: generating the staging version at merge time.
  - If the staging step doesn't need it, drop the type question. It only takes a Choice out of the first Jev request (`TYPE_CRITERIA` in `src/pipeline/store/context.py`).
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
  - an uncertain role;
  - the category `other` (`3`);
  - a `header_context` whose header didn't resolve (`header_context` is null);
  - a context column that describes nothing.
- [ ] **The conflict question** (two context columns claim one column) is the only question asked at upload. It was never seen in the PoC's 12 cases. Check that it stays rare.
- [ ] **header_context uses GLiNER2 + the dictionary** (`src/units/header.py`), and it never guesses.
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

## Command line

- [x] **`incoming` only checks the file:** it saves and merges nothing yet. (Done: an upload is saved through the session, and a message can update or insert from it.)
- [ ] **Only the store schema is saved, not the step sections.** `store` writes the classification into `session.db` (`_store_columns`, `_column_context`, `_export`), and each column's profile (`_store_columns.profile_json`), but not the rest of the sections behind it (the probabilities, the warnings). Every Jev answer with its probabilities is kept in the store's `import.json` (with the user's answers to its questions), outside `session.db`. Warnings are printed once. Decide whether the merge needs them, and save them in `session.db` if it does. The profile describes the data as imported: no request changes it yet, so mark it stale (or recount it) once requests run.
- [ ] **Role, kind and level are stored as integers** (user decision 2026-10-21): the index in `ROLES`, `KINDS` and `LEVELS` (`src/services/storage/store.py`). The order must stay fixed once stores exist. `created_by`/`edited_by` aren't stored yet: `_history` exists (its first row is the import), and they come once requests write to it.
- [x] **No guardrail on `message`** (done: `prompt/guard.py` and `source_route.py`) since the Jev intent check was removed with the chat. Add it back when messages run: is it about the data, and for the store or the incoming file.

## Store upload: questions to the user (review leftovers, all low)

- [ ] **`answer` without `-s` can wait silently** behind another `answer` that is resuming a newer store, then refuse with that store's id. Fix: try the lock without blocking first and say "waiting for another answer on <id>…", or prefer a waiting store over one being resumed.
- [ ] **The keys grouping is untested.** A pause keeps one call's questions per step across all files for keys (the reference call depends on every primary key). Add a test with a primary-key question on one file and a reference question on another.
- [ ] **A notice can describe an answer the user then changes.** A held-back call's notices are still shown (for example "dedicated_context but typed INTEGER: none kept" before the role question is answered). Cosmetic: notices never block.
- [ ] **A kill right after `COMMIT` in `storage.write`** leaves a complete `session.db`: the next retry fails creating its tables and deletes it, and the one after succeeds. Recovery takes two `answer` runs.

## Messages and the session (review leftovers, all low)

- [ ] **A transaction SQLite rolls back by itself** (disk full, I/O error) isn't noticed: the next change opens a fresh one while the session still lists the lost changes. Check `in_transaction` after each request and end the session with an error.
- [ ] **A crash between COMMIT and removing the journal** reports saved changes as lost; resending them applies them twice. Compare the journal with `_history` before reporting.
- [ ] **Two options with the same label** (two numbers both written "10", two identical mentions) collapse into one answer.
- [ ] **A failed run of a waiting message** (Jev down, a crash) drops its answers: the message is sent again and its model calls are paid again. The import keeps its answers in that case.
- [ ] **The final JSON lists every assembly notice,** which may be noisy for the LLM relaying it.
- [ ] **`undo`, `exit` and refusals aren't JSON:** `undo`/`exit` print plain text, refusals go to stderr only.

## Codex review (2026-10-22), filed for later

- [ ] **`import.json` isn't fsynced** (file and folder) after a paid answer: a power cut right after could lose it.
- [ ] **Dates in messages** like `22 octobre 2026` or `22/10/2026` aren't recognised (`operations_jev.py:322`); a numeric date would need a question (22/10 vs 10/22).

## Incoming uploads (review leftovers, low)

- [ ] **A session that crashes with unsaved uploads** leaves their `incoming_pending/<upload>/` folders (with a CSV copy) behind; the journal names the lost upload, but nothing removes the folder.
- [ ] **The matching catalog is the store as committed when the upload started** (kept in the upload's progress): a store change made while an `i_` question is open isn't seen by that upload's matching.
- [ ] **A second upload in the same session can hit "database is locked"** if a large first upload made SQLite spill its page cache (the catalog is read by a separate connection). The snapshot makes it rarer.
- [ ] **Same-named uploads** selected for a message are all kept, with a notice; a real question needs the message flow to pass a user answer back.

## Update / insert from an upload (review leftovers, low)

- [ ] **Keys that differ only by format** (`'7'` and `'007'`, or `' A1'` and `'A1'`) pass the duplicate check as two keys, yet SQLite matches both `'7'` and `'007'` to an INTEGER store key 7 (numeric affinity): an update then takes one of them arbitrarily. Normalise the upload key (trim; for an INTEGER store key, compare as integers) before the duplicate check.
- [ ] **An insert copies the upload key as written** (untrimmed): `' A1'` passes "not in the store" when the store has `'A1'`, then a STRICT TEXT key stores `' A1'` beside it.
- [ ] **An update from several columns of one upload** runs only on rows where every taken cell is filled: a row with one empty cell keeps all its values, even those the file had.

## Prompt to SQL: gaps left for after the first delivery (2026-10-29)

**Where it stands (2026-10-30):** fresh set F (24 client-like requests written before any run, `.bench/sentences/fresh_cases.py`, first run with real Jev calls): **22 / 24 right**, 8 questions to the user in total; of the two wrong, F23 now asks instead of running, F15 still runs a wrong delete. Set F was then used for fixes: the next honest score needs a new untouched set (G), written before any run. Hard sets A–E: **115 right, 4 wrong, 10 held back** (2026-10-29 morning: 94 right, 30 wrong). The 21 regression sets: unchanged all along. Measured on the hard sets A–E (129 requests, `.bench/sentences/attach_cases*.py`, run with `run_today.py`) and the 21 regression sets (174 requests, `.bench/run.py`). A wrong query runs without warning; a held-back request is refused with its reason, so it's safe. Each fix is measured on both before it's committed, two fixes per run.

**Run wrong queries (most urgent):**
- [ ] **A column the request doesn't name:** "no **stock**" (B09: fixed 2026-10-30, a request writing more numbers than its named number columns take also asks about the main table's others; fresh set F15 "nothing in stock and nothing reserved" now finds the quantity, but Jev puts both zeros on it, so it deletes on quantity = 0 alone. Tried on 2026-10-30, none kept: the parse (spaCy attaches "reserved" to the first "nothing"); a question moving a column's second value to an empty named column (it never fires: Jev put both zeros on both columns); showing the 2x question the words after the value (Jev still answers quantity for "nothing reserved", at 0.72 against the 0.7 threshold, and A19, B08 got worse). What's left: a surer threshold for 2x alone, or the user asked whenever two named columns compete for one value) and "lines that are not **empty**" (E14) mean the quantity; "**below** their reorder point" (B04) compares an implied quantity, and comes out reversed. Settled when the column finder ties these words to the quantity column (no word lists in a step).
- [x] **An amount read as a percentage (F23, fresh set):** fixed 2026-10-30: a catalog percentage slot takes a number written as one (or a fraction); a plain "by 20" asks "20% or an amount of 20?".
- [ ] **"Leave the variants without a cost unchanged" is dropped (composed F01, found 2026-11-01):** the formula builds the price right (×1.15 / −5 / the black-ones choice / round to 5 / never above cost×2 / never below cost×1.1), but the exclusion becomes no condition: the only row filter is brand = Orbit, and a variant with no cost would still be raised and rounded (the limits' COALESCE just skips it). The regression test expects `WHERE cost_mad IS NOT NULL AND …` and passes only because every Orbit variant of the demo store has a cost: give the test a row that tells them apart. Settled when "leave … unchanged" / "without a …" becomes a row condition (NOT empty) joined with the filter.
- [ ] **A fill on a text column leaves blanks (E10, runs a wrong change):** "set the size of the variants without a size to one-size" selects the NULL and blank sizes, but the new value `COALESCE(size, 'one-size')` replaces only the NULL ones. `fill_empty` (`operations/builders.py:225`) handles blanks when it gets `column_type = TEXT`, but this change's value doesn't come through the formula's catalog call (passing the type there didn't change it): find where the assignment is built.
- [ ] **"not discounted" (A24)** is read as "discount = ?" (no value) instead of "is empty". The fix is in step 6's operator text ("on an amount, 0 is no value"), but that text is part of every stage-2 question: changing it re-asks all 300 benchmark requests (paid). Do it together with the next change that re-asks them anyway.

**Held back (refused, with the reason):**
- [ ] **Zero words in deletes:** "delete the sales with **no** discount that were refunded" (C05), "**no longer** active" (C13): the "no" is a 0 no condition uses, so the ledger refuses. ("hold **nothing**", C24, fixed 2026-10-30.)
- [ ] **A repeated number on two columns:** "heavier than 250 grams or priced at 250 or more" (B08): the stage-2x question isn't sure which condition takes the second 250.
- [ ] **Several ids:** "variants 101 **or 102**" (D12: the reader never sees "102" as a variant); "variant 101 or variant 109" (D16).
- [ ] **"At least one"** (B13, M02): the existence test doesn't record its own words, so the ledger can't account for the "one".
- [ ] **"fewer than 30 units"** (E23): the 30 is used by no condition.
- [ ] **A value read from another record:** "priced the same as variant 101" (C23): no catalog operation computes it yet.

**Requests relating to each other (seen while preparing the explainer video, 2026-10-30; one run each on an in-memory demo store):**
- [ ] **A swap isn't understood (held back):** "For the Notebook and the Desk Lamp variants, put the cost into the price and the price into the cost." The pipeline keeps it as ONE request, and the formula holds both changes back ("several results are used by no other step": the cost and the price each read twice); nothing changes. Correct reading: two requests on the snapshot, r1 AND r2 (each reads the original values), one statement `UPDATE variants SET price_mad = cost_mad, cost_mad = price_mad WHERE …` (Notebook 30/10 → 10/30, Desk Lamp 400/250 → 250/400). Settled when step 1 cuts a swap in two, or the formula maps "A into B and B into A" to one simultaneous assignment.
- [ ] **"but first" asks twice:** "Show the black variants, then raise their price by 10%, and delete the cancelled sales, but first show every sale." Step 1 reached the right tree, (r1 THEN r2) AND (r4 THEN r3), order r1, r2, r4, r3, but only after two "uncertain link" questions (r1 → r4 at 0.53, r4 → r3 at 0.75 "none"); step 6 also asked about the variant id and noted "a part is missing". A request written last that runs first should be sure without the user.

**Fewer questions to the user (2026-11-01; set G asked 21 questions on 8 of 36 prompts after 01e98c2 and 820a823):**
- [ ] **A threshold per kind of question, set from data.** Every question kind uses the same SURE (0.7). On set G's log (`.bench/benchmark/g_questions.json`), some kinds were right with Jev's likeliest answer almost every time between 0.5 and 0.7 (a word's role 3/3, which column holds a value, what is shown), others came mostly in requests that failed (the formula's links). Log the questions on every set (A–E, F, G: about 200 requests, live, a few dollars), measure per kind how often the top answer at 0.5–0.7 was right, and lower the threshold only for kinds right ≥ 95%; the ledger stays the safety net. No added time per request.
- [ ] **One round of questions instead of several.** A request pauses at the first step with questions, then reruns and may pause again at a later step (2–3 interruptions). Ask a later step's questions in the same round when they don't depend on the earlier answers. Fewer interruptions, not fewer questions; more complex: last.
- Rejected, not to do again: trying every answer in extra pipeline runs and asking only when the SQL differs (questions 32 → 10, but 146 s on some prompts).

**The 21 regression sets: 15 old failures out of 174:** dates (T11, T16, T17), formulas (F03, Y03, E05), linked records (L04, L06, M06), same-column cases (S05, S06), text changes (T03), new operations (N01, N06, N10).

**Not started:** the unified grouping decision; date parts ("per month"); statistics (median, percentiles).

## Production readiness (Codex review, 2026-10-29), filed for after the pipeline

A read-only review (Codex, high reasoning) of the repo for a first pilot delivery. Its verdict was "not ready", for reasons outside the prompt pipeline; the top three were checked in the code. To fix after the pipeline work.

- [ ] **A change runs as soon as it's built** (`src/services/session/server.py:239`): the user sees the changed rows afterwards and can undo until the session ends (commit on exit). That's the chosen design, but the README still says "applies nothing until a human approves", and describes an older product. Decide: a preview and a yes before an UPDATE/DELETE/INSERT runs, keep run-then-undo and fix the README, or a preview only above a row count.
- [ ] **A failed Jev call on the link between two requests** (`src/pipeline/prompt/link.py:205`, `:159`) treats them as independent, with a warning: "select these products, then change their prices" could change a different scope. Settled by a test that simulates the failure on a two-part write and checks that nothing runs.
- [ ] **Export can leave a mixed set of files** (`src/services/storage/export.py:27-46`): each file is replaced safely, but not the set; if file two fails, file one is already replaced. Write every file to a temporary folder, then swap them all.
- [ ] **Store data, the journal and crash logs use the machine's default permissions** (`registry.py:24`, `store.py:117`, `server.py:156`, `crash.py:17`): on a shared machine another user can read them. Restrict the data folder to its owner, and say how to protect `.env`.
- [ ] **No stated store-size limit**: import reads the whole CSV in memory (`services/csv/reader.py:74`), export fetches every row (`pipeline/store/schema.py:249`), a write's `RETURNING` fetches every changed row (`server.py:267`). Test at the pilot client's real file size, then state the limit or stream these paths.
- [ ] **Rehearse the install on a clean machine.** The README was rewritten for the first release (2026-10-30: Python 3.13, uv, Docker and the API key listed; an `orbitrows` alias for `uv run --env-file .env orbitrows`). Still owed: one install from a fresh clone on a machine that has never run it, following only the README.
- [ ] **The Duckling check accepts any service on its port** (`services/duckling.py:24`), and the Docker image is `latest` (`:19`): check a real Duckling answer and pin the image.
- [ ] **Pilot docs**: backup and restore, the supported size, what data is sent to Jev, how to diagnose a failed session.
