# OrbitRows pipeline: steps for expert review

As of 2026-11-02 · written for an expert reviewing where the prompt pipeline can improve.

OrbitRows turns a plain-English request into one checked SQL statement per request, in about 4 seconds (3.6 s median, 5.6 s at the 90th percentile). It runs 8 steps: code does everything that must be exact, and Jev (TypeSafe System One) makes every judgement call as a typed question, a choice or a yes/no, with probabilities. On the 700-case benchmark it got 656 right before the fixes of 2026-11-02 and about 668 after. It refuses rather than guesses: of its failures, only a handful are silent wrong answers.

## The steps at a glance

| # | Step | Module(s) | Decides | Tools | Jev questions on 700 cases |
|---|---|---|---|---|---|
| 0 | Guard, source route | `guard.py`, `source_route.py` | is it a data request; which sources (store, uploads) | Jev | 0 |
| 1 | Requests | `request_graph.py`, `split.py`, `coref.py`, `link.py` | the requests in the prompt, their order, THEN / AND links, "them" resolved | GLiNER2, spaCy, Jev | 27 |
| 2 | Operation | `operation.py` | select / update / delete / insert / alter / create | Jev | (in requests) |
| 3 | Values | `values.py`, `linker.py` | which words are stored values ("Casa" in products.brand), proven by SQL | GLiNER2, meaning model, SQLite, Jev | 43 |
| 4 | Sources | `sources.py` | the main table, and the tables a request reads | Jev | 31 |
| 5 | Columns, parts | `columns.py`, `parts.py` | the columns named; what a read shows, what a change sets | spaCy, meaning model, Jev | 25 |
| 6 | Conditions | `conditions.py` | which rows: one condition per column and written value | number reader, Duckling, Jev | 69 |
| 6b | Formula | `formula.py`, `expressions/`, `operations/` | a change's calculation as a graph of catalog operations | catalog search (bge-small), Jev | 16 |
| 6c | Grouping | `grouping.py` | AND / OR between conditions; sort, limit, groups | spaCy, Jev | 25 |
| 7 | Ledger, assembly | `ledger.py`, `assemble.py` | every written value used; one AST statement per request, validated | code only | 0 |

Per case: about 9 Jev calls, about 20,000 tokens in and 2,500 out. The session then runs the SQL in a savepoint (undo), and nothing is saved until `exit`.

## The rules every step follows

- **Never guess.** An answer below the sure threshold (`SURE = 0.7`, `src/pipeline/prompt/__init__.py`) becomes a question to the user, or to the agent driving the CLI when the user has already settled it.
- **Refuse rather than widen.** A request that can't be built completely is refused with its reason. It never runs with a restriction missing (a dropped condition, a condition not placed, a condition not read).
- **The ledger.** Every number, date and stored value the request writes must be used by exactly one part of it, or the request is refused. One occurrence is one piece of evidence: the same words read by two conditions are refused too.
- **No data read after a message.** What steps know of the values comes from the import profile (`_profile`: counts, distinct values, dates, ranges, comparisons between number columns). Only the final SQL touches the rows.
- **No word lists in steps.** Language knowledge lives in shared readers (numbers in `units/quantity`, dates through Duckling, units through pint); steps don't keep their own lists of English words.
- **Any table, not only a shop.** Wording and rules stay neutral: the store is any set of CSV tables.

## Understanding the request (steps 0–2)

**Step 0, the guard.** One Jev yes/no: is the message a request about the data? If not ("save", "undo", "what can you do"), the CLI names the command to use. A doubt lets the pipeline run: a data request is never stopped on uncertainty.

**Step 1, requests.**
1. spaCy proposes mentions; Jev confirms which words point to others ("them", "the ones with no stock").
2. GLiNER2 proposes cuts between actions; Jev confirms each cut. A weak cut keeps the text together.
3. Jev links the requests: THEN (one waits for the other's rows or state) or AND (both read the original snapshot).

*Where it fails:* "but first" and reversed orders need several questions; a swap ("cost into price and price into cost") is kept as one request and refused; 27 questions on 700 cases, nearly all on two-request prompts ("delete the cancelled sales, and show the pending ones").

**Step 2, operation.** Jev picks the statement type per request; below 0.8 the likeliest is kept and flagged.

## Finding the data (steps 3–5)

**Step 3, values.** GLiNER2 proposes value phrases; Jev classifies each (a value to look up, a new value, a number, not a value); SQLite proves where a looked-up value is stored.

*Where it fails:* the most frequent question of all is "role uncertain" on numbers of a range ("a quantity from 2 to 3", 36 questions, right 75% of the time when taken). A brand also stored as a supplier's name ("not from PaperHub") is asked, and the likeliest answer was wrong.

**Step 4, sources.** Jev picks the main table and the tables read. *Where it fails:* "which products were sold in September" chose sales, not products (14 "table uncertain" questions, right 71%).

**Step 5, columns and parts.** The column finder (code, no Jev) matches words to column names: by lemma, by meaning (multilingual MiniLM), by kind. Jev then says, per column, whether a read shows it or a change sets it.

*Where it fails:*
- The finder works on lemmas, so "costing" counted as the name "cost". Since 2026-11-02 a column is "named" only when its name is written as it is (plurals allowed); a resemblance brings the same-unit columns of its table as one choice.
- Columns have no description: their meaning reaches Jev only through their sql names. "Costing the customer" (the price) stays read as the cost, at 0.98, in 3 of 6 cases.
- Verbs that name no column: "sales **selling** at least 1" (quantity): the number is then unused, and the ledger refuses (5 cases).

## Choosing the rows (step 6)

Step 6 runs in stages, each one Jev call:

1. **Stage 1, the columns.** Per candidate column, "does the request choose rows by it?" (yes/no), or one choice among look-alike columns. Candidates: the named columns, the main table's id, date columns when a date is written, columns a written unit converts to, the main table's other number columns when the request writes more numbers than named columns take, and the linked tables ("has a matching record in…", "has none", "how many").
2. **Stage 2, one condition per written value:** operator, operand (a number from the shared reader, a date from Duckling, a stored value, another column), negation.
3. **Later stages, together:** 2b computed values ("more than twice their cost"), 2c linked records ("at least 3 variants"), 2d conditions on groups, 2n negations confirmed as built, 2x which column takes words two conditions both read.

*Where it fails (69 questions, the most of any step):*
- **Stage 1 asks about the id on plain value filters.** "Which variants are white?" asks "chooses by variant_id?" (26 questions, almost never needed).
- **Dates.** "Before October 5" went unread in one sentence: the condition was built with nothing, and the read ran on every sale (now refused, the date reading still open). A plain number on a number column ("lead time more than 8") is sent to the date reader as a duration and refused (3 cases).
- **"Has no … in Casablanca".** The right query puts the condition inside the "has no" test (no line whose warehouse is Casablanca); the pipeline drops the test and keeps the condition, which inverts the meaning. The ledger refuses it today.
- **Conditions on groups.** "Colours with more than 2 variants": no group condition is built at all (3 cases, refused).
- **A near tie refused before it is asked.** "Lines with no units": the value's answer was 0.47 against 0.42; the ledger refused the request before a question could settle it.

## Computing and building (formula, grouping, ledger, assembly)

**Formula (6b).** Pass 1 cuts a change into pieces (a calculation, a branch, a rounding, a limit, a filter); each piece is matched by meaning to a tested catalog operation (`operations.db`, searched with bge-small, then chosen by Jev, "none" allowed); pass 2 links which result feeds which piece and builds one SQL expression (`CASE`, `ROUND`, `MIN`, `MAX`). A broken map is a question. *Where it fails:* "never below their cost" is sometimes also read as a row condition, which filters instead of capping (four 50/50 questions; the case flips between runs).

**Grouping (6c).** Jev answers, per pair of conditions, AND or OR; the tree that fits every answer is built, checked against the sentence's grammar. *Where it fails:* the same pair can be asked twice and answered both ways ("October sales that were completed or refunded" built as "(October and refunded) or completed").

**Ledger and assembly (7).** Code only. The ledger accounts every written value (since 2026-11-02, a linked test claims its own count: "at least **one** white variant", "**no** sales"). Assembly builds one AST per request, lifts conditions on other tables into `IN (SELECT …)` chains along the foreign keys, and validates the SQL on the store.

## Measured: 700 cases against Codex

The set (`.bench/cases700.py`): 548 reads, 110 updates, 42 deletes; each with an expected result checked on a demo store. Codex ran as normally used (gpt-6-sol, medium reasoning, the schema and the request).

| | Pipeline (Jev) | Codex |
|---|---|---|
| Right | 656 (93.7%); about 668 after 2026-11-02 | 660 (94.3%) |
| Wrong | 19 (4 silent; 15 after a question whose likeliest answer the benchmark took) | 39, all silent |
| Refused with a reason | 25 | never refuses |
| Time per case | 4.0 s | 6.2 s |

The two fail mostly on different cases: only 6 are wrong for both.

What still needs pipeline work (about 27 cases):

| Kind | Cases | Cause |
|---|---|---|
| Refused | 6 | an unusual verb not tied to its column ("selling"), one number read by two columns |
| Refused | 3 | a plain number sent to the date reader ("lead time more than 8") |
| Refused | 3 | no condition on groups built ("colours with more than 2 variants") |
| Refused | 2 | "has no … in Casablanca": the condition belongs inside the test |
| Refused | 2 | a table read as the main one (H407); a date not read (H185) |
| Wrong after a question | 6 | brand vs supplier, "not stored in", OR placement, products vs sales, a lost empty-value condition |
| Wrong, silent | 3 | "costing the customer" read as the cost |

4 to 5 more are scoring artefacts (a right answer without the key column; "Volt" and "volt" counted as one brand).

## Where an expert could look

Ranked by what each would fix, most first.

1. **What columns mean.** Jev only sees sql names (and, since 2026-11-02, the profile's ranges and comparisons). Short descriptions per column, written once at import by the agent or the user for look-alike columns, would answer the remaining silent errors and similar ambiguities. Open: who writes them, how they're kept current.
2. **The context Jev is given.** The same question was answered at 0.64 with only the request, and at 0.98 inside the pipeline's full state (every table, every column, the other requests). Which state each question kind gets is a design choice nobody has tuned.
3. **Thresholds per question kind.** One threshold (0.7) for every kind. Some kinds are right at 0.5–0.7 almost always (the request links: 10 of 10; "whole records": 7 of 7); others are not (step 5's "what is shown": 6 of 11). Measured per kind, the questions could drop without new errors.
4. **Questions that should never be asked.** "Chooses by variant_id?" on a value filter (26) and "role uncertain" on a written range (36): a quarter of all questions.
5. **Conditions inside linked tests and on groups.** "Has no line in Casablanca", "colours with more than 2 variants": the condition has to be placed inside a subquery or a HAVING. Today these are refused.
6. **Words that name columns indirectly.** Verbs ("selling", "holding", "weighing"), set phrases ("set aside for orders"). The column finder has lemma, meaning and kind; none covers these.
7. **Near ties before refusal.** A 0.47 / 0.42 answer should become a question before the ledger refuses.
8. **Benchmark method.** Cases with questions near 0.5 flip between runs, and the benchmark takes the likeliest answer. Settled answers, or several runs per case, would separate a fix's effect from noise.

Detailed write-ups: `doc/to-review.md` (gaps by step, the "which column a request's words mean" limitations L1–L9), `ARCHITECTURE.md` (the design), `.bench/runs/2026-11-02_cases700/` (raw benchmark records, with every question, Jev call and step output).
