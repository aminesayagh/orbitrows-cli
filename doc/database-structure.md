# Pilotstore: database structure

This is a review document. It describes the PostgreSQL structure behind the flow, and explains, table by table, which need it serves. The diagram is the "Database structure" frame in `pilotstore.excalidraw`.

## 1. The flow the structure serves

1. **The user uploads the store** (a CSV exported from the CMS).
   - It's profiled into columns: schema and context.
   - Uploading again means starting over.
2. **The user uploads an incoming** (a supplier file).
   - Its columns are profiled.
   - Jev links them to the store columns, with a role and a level.
3. **The user edits the store, the incoming, or both, by prompt, at any time.**
   - An edit can change cells, row order, column headers, column order and column context.
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

- **Identity and state are separate.**
  - An identity table (`…_record`, `…_column`) holds only what never changes: the id, the parent, and the column `key`.
  - Everything that can change lives in a revision table: cells, position, header, schema, context…
- **Creating anything writes the identity and its first revision together,** in one statement (`edit_id = null` for the upload).
- **Revisions are append-only.** No row is ever updated:
  - an edit adds revisions;
  - the latest revision is the current state;
  - a delete is a revision too (`cells = null` for a record, `deleted = true` for a column).
- **Every fact is stored once.** A version ends when the next one begins, so nothing is duplicated and nothing has to be kept in sync.
- **One timeline per store.** A single `edit` table serves the store and its incomings, so one prompt can edit both tables as one edit.
- **An edit is a single SQL statement.** The edit row and all its revisions are written together, so it succeeds or fails as a whole.
- **Undo is a single statement:** `DELETE` the edits after a point. `ON DELETE CASCADE` removes every revision they wrote.
- **Every reference is a real foreign key, and it always points to an identity, never to a revision.** References stay valid through renames, moves, deletes and undos. Uniqueness is enforced by constraints, not by application code.
- **The store and incoming data tables are duplicated on purpose.** Their parents, lifetimes and cascades differ. The versioning logic is shared in application code, not in the schema.
- **The merge is an edit, not a separate staging table.** Its result is the next version of the store.

## 3. Tables

### `store` / `incoming`

| Table | Fields |
|---|---|
| `store` | id (primary key), created_at |
| `incoming` | id (primary key), store_id (ref), created_at |

**Need:**
- `store` is the root of one working session. Everything else belongs to a store and is deleted with it;
- an `incoming` is one supplier file imported into that session.

### `edit`

| Field | Type | Note |
|---|---|---|
| id | int, primary key | increasing: one timeline per store, which also gives the order of the edits |
| store_id | ref | the session. `ON DELETE CASCADE` |
| source | int | 0 prompt, 1 merge, 2 profile, 3 manual (extendable) |
| prompt | text, null | the user's request (prompt source) |
| update | jsonb, null | the MongoDB update JSON executed |
| incoming_id | ref, null | merge source: the incoming that was merged |
| created_at | date | |

**Need:**
- the history of what changed, why, and from which source;
- the anchor for undo;
- **the single table all four revision tables and `merge_row` point to.** That's a plain foreign key: no reference has to target one of two tables.

**What it pins by itself:**
- **A prompt that edits the store and the incoming** writes revisions on both sides under one `edit_id`, so it's undone as one edit.
- **A merge at edit k saw the store and the incoming exactly as they were before k.** Its place in the timeline pins both versions, so no "merged version" field is needed.

### `store_column` / `incoming_column` (identity)

| Field | Type | Note |
|---|---|---|
| id | int, primary key | the foreign-key target (`column_match`, `context_client`) |
| store_id / incoming_id | ref | |
| key | string | `c1`, `c2`…: the key used inside `cells`. Never changes, unique per store/incoming |

**Need:**
- a stable identity for each column;
- `key` is separate from `id` because every file has a `c1`, so a string key can't be a global identifier;
- `cells` is keyed by `key`, never by header text or position. So **renaming, moving or deleting a column never touches a record.**

### `store_column_revision` / `incoming_column_revision`

| Field | Type | Note |
|---|---|---|
| column_id | ref | `ON DELETE CASCADE` |
| edit_id | ref, null | null = as uploaded. `ON DELETE CASCADE` |
| header | string | the column name used in the export; `""` = no header |
| position | numeric | column order; a midpoint on a move |
| deleted | bool | column deleted by this edit |
| context_classify | int | 0 dedicated_context, 1 header_context, 2 both, 3 none, 4 uncertain |
| context_category | int, null | 0 currency, 1 physical_unit, 2 counting_unit, 3 compound_unit, 4 percentage, 5 language, 6 other, 7 unknown (see §7). Set on the column that carries the context |
| context_client | ref, null | the column (identity) carrying this column's context, e.g. `Devise` for `Prix` |
| header_context | string, null | unit or currency read from the header (`Poids (kg)` → `kg`) |
| schema | jsonb | store only: a Table Schema field, `{"name": header, "type": …}`, the type classified by Jev (`string`, `enum`, `integer`, `number`, `boolean`, `date`, `datetime`) |

`UNIQUE NULLS NOT DISTINCT (column_id, edit_id)`

**Need:** everything about a column that can change.
- **The context is in the revision because it is read from the header.** Renaming `Poids (kg)` to `Poids (g)` changes the unit. The rename edit re-runs the context detection for that column (one small Jev request), and writes the new header and new context in one revision. Undo then restores both together.
- **`deleted` is a separate flag** because an empty header is legitimate (a file with no headers).
- **`schema` is versioned like the rest.** A later source (re-profiling, a manual fix) can change it without special handling.

### `column_match`

| Field | Type | Note |
|---|---|---|
| incoming_column_id | ref | incoming column identity |
| store_column_id | ref | store column identity |
| link | int | 0 direct, 1 partial (partial triggers the split algorithm) |
| confidence | real | confidence of the link |
| role | int | 0 dedicated_key, 1 descriptive_base, 2 qualifier, 3 none |
| role_confidence | real | |
| level | int, null | 0 individual_record, 1 product_family (keys only) |
| level_confidence | real, null | |

**Need:**
- the validated mapping between the two files;
- the merge reads it to know which incoming columns feed which store fields, how (`link`), and how to search (`role`, `level`);
- because it points to column identities, **a rename doesn't redo the matching.** A new matching after a rename is an explicit re-run.

### `store_record` / `incoming_record` (identity)

| Field | Type |
|---|---|
| id | int, primary key |
| store_id / incoming_id | ref |

**Need:**
- a stable, unique identity that other tables can reference with a real foreign key (`merge_row`);
- nothing else: whether it exists at a given version is answered by its revisions.

### `store_record_revision` / `incoming_record_revision`

| Field | Type | Note |
|---|---|---|
| record_id | ref | `ON DELETE CASCADE` |
| edit_id | ref, null | null = the uploaded content. `ON DELETE CASCADE` |
| position | numeric | row order |
| cells | jsonb, null | `{"c1": "TS-ORG-BLK-M", "c9": "15.90"}`; null = deleted by this edit |

`UNIQUE NULLS NOT DISTINCT (record_id, edit_id)`: at most one revision per record per edit, and exactly one upload revision.

**Need:** the content of each record over time.
- **Upload:** writes one revision per record, with `edit_id = null`. So a record that is never edited still has its position and cells.
- **Edit:** writes a revision only for the records it changes, moves, deletes or adds.
- **Undo:** deleting an edit removes its revisions, so the previous ones become current again. A record an undone edit had created is left with no revision, so it no longer exists in any version.

**Rules for `cells`:**
- **Values are the raw strings, as written in the CSV.** `19.90` stays `19.90`, and `007` keeps its zeros. JSON numbers would lose both.
- **Empty cells are left out.** A missing key means empty.
- **Keys of deleted columns stay in `cells`.** The export skips them, and undoing the delete brings them back.
- **Indexed with `GIN (cells jsonb_path_ops)`.** A Mongo equality filter `{"c3": "Black"}` becomes `cells @> '{"c3": "Black"}'`, which this index serves. Range filters cast (`(cells->>'c9')::numeric`) and scan: milliseconds at 20,000 rows.

**Rules for `position`** (records and columns):
- the upload order is `1..n`;
- gaps after deletes are fine;
- a manual move takes the midpoint between neighbours (`3.5`), so only one revision is written;
- `numeric` is exact, so the midpoints never collide, as floats eventually would;
- a sort ("sort by price") is a view, applied with `ORDER BY` at read time. It's never stored, and the CMS import ignores row order anyway.

### `merge_row`

| Field | Type | Note |
|---|---|---|
| edit_id | ref | the merge edit. `ON DELETE CASCADE` |
| incoming_record_id | ref | the supplier row (identity) |
| record_id | ref, null | the store record (identity) it updated; null = no product matched |
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
| **Upload a store** | `store`; per column: identity + first revision; per row: identity + first revision (`edit_id = null`) | one transaction |
| **Upload an incoming** | the same, under `incoming` | one transaction |
| **Prompt edit (store, incoming, or both)** | one `edit` + a revision per changed, moved, deleted or added record or column (+ identities for added ones) | 1 |
| **Rename a column** | one column revision: new header + re-detected context | 1 |
| **Move a column** | one column revision: new position (midpoint) | 1 |
| **Delete a column** | one column revision with `deleted = true`; no record changes | 1 |
| **Merge** | one `edit` (source merge, with `incoming_id`), the revisions of the changed store records, one `merge_row` per supplier row × matched record | 1 |
| **Fix the merge by prompt** | an ordinary edit, after the merge | 1 |
| **Reject the merge** | `DELETE FROM edit WHERE store_id = $1 AND id >= <merge>`, which also removes the fixes made after it | 1 |
| **Undo back to edit k** | `DELETE FROM edit WHERE store_id = $1 AND id > k` | 1 |
| **Export** | the current records ordered by position; the current columns (not deleted) ordered by position, with their headers | read only |

**Reading.** One pattern answers every version question, for records and for columns:

```sql
SELECT * FROM (
  SELECT DISTINCT ON (record_id) * FROM store_record_revision
  WHERE edit_id IS NULL OR edit_id <= $k        -- drop this line for the current state
  ORDER BY record_id, edit_id DESC NULLS LAST   -- latest edit first, upload last
) cur WHERE cells IS NOT NULL ORDER BY position;
```

For columns, it's the same query on `…_column_revision`, with `DISTINCT ON (column_id)` and `WHERE NOT deleted`.

- **An identity exists at version k** if it has a revision at or before k, and that revision isn't a delete.
- **What edit k changed:** the revisions with `edit_id = k`, each next to the previous revision of the same identity.
- **Is a merge out of date?** Yes if some edit after the merge wrote incoming revisions for the merged incoming.

## 5. Decisions and rejected alternatives

| Decision | Rejected alternative | Why |
|---|---|---|
| One JSONB object per row | One row per cell (EAV); the whole table in one JSONB | EAV means 600k rows per import and a pivot to read a record; a whole-table document is rewritten on every edit and can't be indexed per row |
| Append-only revisions | `from_edit` + `to_edit` validity ranges | The end of a version would be stored twice, and each edit would need an update plus an insert kept in sync |
| Identity + revision tables | One table keyed by `(record_id, from_edit)` | `record_id` alone wasn't unique, so nothing could reference a record with a real foreign key |
| Identities hold only immutable fields | `edit_id` ("created by") on the identity; position, header, context or schema on the identity | `edit_id` in two places made versions harder to read and maintain; anything mutable on the identity can't be undone |
| Always identity + first revision | Content only on edits | One rule for every version question; a never-edited record or column still has its full state |
| Column context in the column revision | Context on the identity, recomputed after a rename or undo | Undo would no longer be a single `DELETE` |
| `deleted` flag on column revisions | `header = null` means deleted | An empty header is legitimate (files with no headers) |
| No upload edit (`edit_id = null`) | The upload as edit #1 | Re-uploading means starting over. Null covers the one upload |
| `position numeric` in the revision | `int × 100` gaps; `float`; `before`/`after` linked list; position on the identity | Gaps run out after about 6 inserts; floats collide silently; a linked list needs recursive reads and breaks on one bad pointer; a position on the identity can't be undone |
| One `edit` table for store and incoming | `store_edit` + `incoming_edit` | A prompt can edit both tables; one timeline undoes it as one edit and pins the merged versions |
| `source` on the edit | Inferring the type from null fields | With more than two sources (prompt, merge, profile, manual…), null fields can't tell them apart |
| No `incoming_edit_id` on the merge | Storing the merged incoming version | The merge's place in the single timeline already pins it |
| The merge is an edit | A separate staging table + staging columns | The staging table *is* the next store version; separate tables would duplicate columns and maintenance |
| No `approved_at` | An approval timestamp | Approval is the export; `created_at` dates the merge |
| Store and incoming data tables duplicated | Generic `dataset_*` tables with a `kind` | A foreign key can't target one of two tables; cascades and lifetimes differ |
| A sort is not stored | Persisted order after a sort | It's view state; storing it rewrites every position, and the CMS ignores row order |

## 6. Accepted limits

- **Undo is a stack, for the whole session.**
  - Going back to edit k discards every edit after it, on the store *and* on every incoming.
  - Removing only edit 3 while keeping 4 and 5 is impossible, because 4's filter was evaluated on the state 3 produced. The stored prompts allow re-running 4 and 5.
  - If parallel imports on one store become a real use case, the fix is a timeline per incoming, but not before.
- **Rejecting a merge also discards the fixes made after it.**
- **Reading the current state uses `DISTINCT ON`** instead of a plain `WHERE`. That's milliseconds at 20,000 records. If it's ever slow, add a materialized view refreshed after each edit.
- **The database can't enforce "every identity has at least one revision",** because the rule spans two tables. It's guaranteed by writing both in the same statement.
- **Undo leaves identity rows with no revisions** (records or columns created by an undone edit). Every query ignores them; a cleanup job is optional.
- **The index covers all revisions,** not only the current ones, so a filter's hits must then be checked against the current state.
- **A column change copies the whole column state,** `schema` included: a few hundred bytes per edited column.

## 7. Open points

- **Diagram, minor:**
  - `incoming_record_revision.cells` should be `jsonb|null`;
  - `header_context` should be `string|null` on both column revisions;
  - the cardinality labels aren't consistent. Arrows go from child to parent, so they should all read `n x 1`. `merge_row → incoming_record` is labelled `1 x 1`, but it's `n x 1`, because a family key writes several rows for one supplier row;
  - one zero-length arrow is left above `column_match`;
  - the constraints (`unique nulls not distinct`, `on delete cascade`) are listed in this document only.
- **`column_match` isn't versioned.** If the user can later edit or validate links as tracked edits, it follows the same pattern: an identity and its revisions.
- **Context categories. Decided in the CLI: every category is stored as is, never folded into another** (0 currency, 1 physical_unit, 2 counting_unit, 3 compound_unit, 4 percentage, 5 language, 6 other, 7 unknown). Jev's category Choice and the header dictionary use these names directly. A column in `other` or `unknown` isn't flagged at upload; the merge flags it only if it uses that column.
- **Several context columns claiming one column. Decided in the CLI: the user is asked at upload.** `context_client` holds a single reference. When two context columns both get a yes from Jev (≥ 0.5), the CLI lists them by Jev confidence, plus "none", and stores the user's answer. This is the only question asked at upload.
- **Prompt execution:** the Mongo update executor must only accept field operators (`$set`, `$unset`, `$rename`…) with plain filters. It must reject `$where` and `$function`, which run JavaScript.
