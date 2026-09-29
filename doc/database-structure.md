# Pilotstore: database structure

This is a review document. It describes the PostgreSQL structure behind the flow, and explains, table by table, which need it serves. The diagram is the "Database structure" frame in `pilotstore.excalidraw`.

## 1. The flow the structure serves

1. **The user uploads the store** (a CSV exported from the CMS).
   - It's profiled into columns: schema and context.
   - Uploading again means starting over.
2. **The user uploads an incoming** (a supplier file).
   - Its columns are profiled.
   - Jev links them to the store columns, with a role and a level.
3. **The user edits the store or the incoming by prompt, at any time.**
   - A prompt becomes a MongoDB-style update JSON.
   - Every edit can be undone.
4. **The user requests the merge.**
   - The incoming is converted to the store's format.
   - It's matched to store records and written as a new version of the store.
   - What couldn't be done is reported.
5. **The user reviews the merge.** They compare the store before and after it and the incoming rows, then either:
   - fix it by prompt;
   - reject it (undo);
   - export the current store as CSV, to be re-imported by the CMS, which validates it.

**No live system is written.** The store is a copy, and the CMS import is the final gate.

## 2. Design principles

- **Identity and content are separate.** A `…_record` is a record's identity and is never updated. Its content lives in `…_record_revision` rows.
- **Revisions are append-only.** No row is ever updated:
  - an edit adds revisions;
  - the latest revision is the current state;
  - a revision with `cells = null` is a delete.
- **Every fact is stored once.** A version ends when the next one begins, so nothing is duplicated and nothing has to be kept in sync.
- **An edit is a single SQL statement.** The edit row and all its revisions are written together, so it succeeds or fails as a whole.
- **Undo is a single statement:** `DELETE` the edits after a point. `ON DELETE CASCADE` removes everything they wrote.
- **Every reference is a real foreign key.** Uniqueness is enforced by constraints, not by application code.
- **The store and incoming sides are duplicated on purpose.** Their parents, lifetimes and cascades differ. The versioning logic is shared in application code, not in the schema.
- **The merge is a store edit, not a separate staging table.** Its result is the next version of the store.

## 3. Tables

### `store`

| Field | Type |
|---|---|
| id | int, primary key |
| created_at | date |

**Need:** the root of one working session. Everything else belongs to a store and is deleted with it.

### `store_column` / `incoming_column`

| Field | Type | Note |
|---|---|---|
| id | int, primary key | the foreign-key target (from `column_match`) |
| key | string | `c1`, `c2`…: the key used inside `cells`; unique per store/incoming |
| store_id / incoming_id | ref | |
| header | string | original column name, for the CSV export *(to add)* |
| position | int | column order in the export *(to add)* |
| …_context_client | ref | the context column that describes this column (e.g. `Devise` for `Prix`) |
| …_context_category | int | 0 currency, 1 unit, 2 language |
| header_context | string | unit or currency read from the header (`Poids (kg)` → `kg`) |
| context_classify | int | 0 dedicated_context, 1 header_context, 2 both, 3 none, 4 uncertain |
| schema | jsonb | store only: type, decimals, unit, allowed values… (step 0 profiling) |

**Need:**
- one row per column, with what the profiling and Jev learned about it;
- `key` is separate from `id` because every file has a `c1`: a string key can't be a global identifier;
- headers can be empty or duplicated, so `cells` is keyed by `key`, never by header text.

### `column_match`

| Field | Type | Note |
|---|---|---|
| incoming_column_id | ref | |
| store_column_id | ref | |
| link | int | 0 direct, 1 partial (partial triggers the split algorithm) |
| confidence | real | confidence of the link |
| role | int | 0 dedicated_key, 1 descriptive_base, 2 qualifier, 3 none |
| role_confidence | real | |
| level | int, null | 0 individual_record, 1 product_family (keys only) |
| level_confidence | real, null | |

**Need:**
- the validated mapping between the two files;
- the merge reads it to know which incoming columns feed which store fields, how (`link`), and how to search (`role`, `level`).

### `store_record` / `incoming_record`

| Field | Type | Note |
|---|---|---|
| id | int, primary key | |
| store_id / incoming_id | ref | |
| edit_id | ref, null | the edit that created the record; null = uploaded. `ON DELETE CASCADE` |

**Need:**
- a stable, unique identity that other tables can reference with a real foreign key (`merge_row`);
- `edit_id` lets undo remove records a prompt added, together with their revisions, so no identity is ever left without content.

### `store_record_revision` / `incoming_record_revision`

| Field | Type | Note |
|---|---|---|
| record_id | ref | `ON DELETE CASCADE` |
| edit_id | ref, null | the edit that wrote this revision; null = the uploaded content. `ON DELETE CASCADE` |
| position | numeric | row order |
| cells | jsonb, null | `{"c1": "TS-ORG-BLK-M", "c9": "15.90"}`; null = deleted by this edit |

`UNIQUE NULLS NOT DISTINCT (record_id, edit_id)`: at most one revision per record per edit, and exactly one upload revision.

**Need:** the content of each record over time.
- **Upload:** writes one revision per record, with `edit_id = null`. So a record that is never edited still has its position and cells.
- **Edit:** writes a revision only for the records it changes, moves, deletes or adds.
- **Undo:** deleting an edit removes its revisions, so the previous ones become current again.

**Rules for `cells`:**
- **Values are the raw strings, as written in the CSV.** `19.90` stays `19.90`, and `007` keeps its zeros. JSON numbers would lose both.
- **Empty cells are left out.** A missing key means empty.
- **Indexed with `GIN (cells jsonb_path_ops)`.** A Mongo equality filter `{"c3": "Black"}` becomes `cells @> '{"c3": "Black"}'`, which this index serves. Range filters cast (`(cells->>'c9')::numeric`) and scan: milliseconds at 20,000 rows.

**Rules for `position`:**
- the upload order is `1..n`;
- gaps after deletes are fine;
- a manual move takes the midpoint between neighbours (`3.5`), so only one revision is written;
- `numeric` is exact, so the midpoints never collide, as floats eventually would;
- a sort ("sort by price") is a view, applied with `ORDER BY` at read time. It's never stored, and the CMS import ignores row order anyway.

### `store_edit` / `incoming_edit`

| Field | Type | Note |
|---|---|---|
| id | int, primary key | increasing, so it also gives the order of the edits |
| store_id / incoming_id | ref | |
| prompt | text | the user's request |
| update | jsonb | the MongoDB update JSON executed |
| confidence | real | *(to define: what it measures)* |
| incoming_id | ref, null | store only: null = prompt edit, set = merge |
| incoming_edit_id | ref, null | store only: the incoming version merged; null = the incoming as uploaded |
| created_at | date | |

**Need:**
- the history of what changed and why;
- the anchor for undo;
- a store edit with `incoming_id` set **is the merge**.
- **Both merge fields are needed.** `incoming_edit_id` alone can't identify a merge, because an unedited incoming is merged with `incoming_edit_id = null`.

### `merge_row`

| Field | Type | Note |
|---|---|---|
| edit_id | ref | the merge edit. `ON DELETE CASCADE` |
| incoming_record_id | ref | the supplier row |
| record_id | ref, null | the store record it updated; null = no product matched |
| issues | jsonb, null | per-cell problems: `{"c9": {"status": "unresolved", "reason": "no_rate"}}`; null = clean |

`UNIQUE NULLS NOT DISTINCT (edit_id, incoming_record_id, record_id)`

**Need:** the row-to-row result of a merge. The merge edit only links whole files, and revisions exist only where values changed. `merge_row` covers what revisions can't:
- **which supplier row produced a store change,** for the review screen;
- **a match whose values didn't change:** no revision, but the match is recorded;
- **a match where every cell is unresolved:** no revision, but the issues are attached to the product;
- **a family key updating 5 variants:** 5 rows with the same `incoming_record_id`;
- **a supplier row that matched nothing:** `record_id = null`, and it's still reported.

It stores no Jev confidence: Jev decides the matching, not the writing of cells.

## 4. Scenarios

| Scenario | What is written | Statements |
|---|---|---|
| **Upload a store** | `store`, `store_column`s, one `store_record` + one revision (`edit_id = null`) per row | one transaction |
| **Prompt edit on the store** | one `store_edit` + one revision per changed, moved or deleted record (+ `store_record` for added ones) | 1 |
| **Merge** | one `store_edit` (with `incoming_id`), the revisions of the changed records, one `merge_row` per supplier row × matched record | 1 |
| **Fix the merge by prompt** | an ordinary store edit, after the merge | 1 |
| **Reject the merge** | `DELETE FROM store_edit WHERE store_id = $1 AND id >= <merge>`, which also removes the prompt fixes made after it | 1 |
| **Undo back to edit k** | `DELETE FROM store_edit WHERE store_id = $1 AND id > k` | 1 |
| **Export** | read the current state, ordered by position, header from `store_column` | read only |

**Reading:**
- **Current state:**
  ```sql
  SELECT * FROM (
    SELECT DISTINCT ON (record_id) * FROM store_record_revision
    ORDER BY record_id, edit_id DESC NULLS LAST   -- latest edit first, upload last
  ) cur WHERE cells IS NOT NULL ORDER BY position;
  ```
- **State after edit k:** the same query, with `AND (edit_id IS NULL OR edit_id <= k)` inside.
- **What edit k changed:** the revisions with `edit_id = k`, each next to the previous revision of the same record.
- **Is a merge out of date?** Its `incoming_edit_id` differs from the incoming's latest edit.

## 5. Decisions and rejected alternatives

| Decision | Rejected alternative | Why |
|---|---|---|
| One JSONB object per row | One row per cell (EAV); the whole table in one JSONB | EAV means 600k rows per import and a pivot to read a record; a whole-table document is rewritten on every edit and can't be indexed per row |
| Append-only revisions | `from_edit` + `to_edit` validity ranges | The end of a version would be stored twice, and each edit would need an update plus an insert kept in sync |
| Identity + revision tables | One table keyed by `(record_id, from_edit)` | `record_id` alone wasn't unique, so nothing could reference a record with a real foreign key |
| No upload edit (`edit_id = null`) | The upload as edit #1 | Re-uploading means starting over. Null covers the one upload |
| `position numeric` in the revision | `int × 100` gaps; `float`; `before`/`after` linked list; position on the identity | Gaps run out after about 6 inserts; floats collide silently; a linked list needs recursive reads and breaks on one bad pointer; a position on the identity can't be undone |
| The merge is a store edit | A separate staging table + staging columns | The staging table *is* the next store version; separate tables would duplicate columns and maintenance |
| No `kind` on edits | A `kind` column | A non-null `incoming_id` already means a merge |
| No `approved_at` | An approval timestamp | Approval is the export; `created_at` dates the merge |
| Store and incoming tables duplicated | Generic `dataset_*` tables with a `kind` | A foreign key can't target one of two tables; cascades and lifetimes differ |
| A sort is not stored | Persisted order after a sort | It's view state; storing it rewrites every position, and the CMS ignores row order |

## 6. Accepted limits

- **Undo is a stack.** Going back to edit k discards every edit after it. Removing only edit 3 while keeping 4 and 5 is impossible, because 4's filter was evaluated on the state 3 produced. The stored prompts allow re-running 4 and 5.
- **Rejecting a merge also discards the prompt fixes made after it.**
- **Reading the current state uses `DISTINCT ON`** instead of a plain `WHERE`. That's milliseconds at 20,000 records. If it's ever slow, add a materialized view refreshed after each edit.
- **The database can't enforce "every record has at least one revision",** because the rule spans two tables. It's guaranteed by writing both in the same statement (upload, and prompts that add records).
- **The index covers all revisions,** not only the current ones, so a filter's hits must then be checked against the current state.

## 7. Open points

- **The two `store_edit` arrows** (to `incoming` and to `incoming_edit`) aren't drawn yet.
- **`header` and `position` on `store_column` / `incoming_column`** aren't on the diagram yet.
- **`confidence` on `store_edit` / `incoming_edit`:** what it measures isn't defined yet.
- **Confidences are drawn as `int`.** Decide between `real` (0–1) and a percentage.
- **Column edits by prompt** (add or rename a column) aren't versioned. They'd need `store_column` revisions; that's out of scope until prompts can change columns.
- **Prompt execution:** the Mongo update executor must only accept field operators (`$set`, `$unset`, `$rename`…) with plain filters. It must reject `$where` and `$function`, which run JavaScript.
