# Incoming-file acceptance pack (proposed v1)

This pack is a target for the incoming-file workflow. The current `incoming` command checks a CSV but does not save it,
so the 20 prompts in [prompts.json](prompts.json) are acceptance cases to implement, not passing CLI tests yet.

## Files

| Role | File | Key and purpose |
|---|---|---|
| Store | `store/products.csv` | `product_id`; product names, brands and status |
| Store | `store/variants.csv` | `sku`; variant price, stock and barcode; `product_id` refers to products |
| Incoming | `incoming/supplier_offer.csv` | one row per SKU; suggested prices and one new SKU (`NEW-CUP`) |
| Incoming | `incoming/warehouse_counts.csv` | one row per SKU and warehouse; counts must be summed by SKU |
| Incoming | `incoming/competitor_prices.csv` | one or more quotes per SKU; comparison source only in these prompts |

All amounts are MAD. Every incoming file is read-only; a prompt may use it to compare or to propose a change to a store
table. Nothing in an incoming file changes on apply. A change must show its affected rows and require explicit approval
before the store is saved. `reads` and `writes` in `prompts.json` are the expected source and target resolution; `expected`
is the result to assert, independent of the SQL text or AST shape.

The cases deliberately contain an accented name and a comma in a quoted name, a blank recommended price, an unmatched
supplier SKU, an unmatched store SKU, a mismatched barcode, repeated warehouse and competitor SKUs, and an unknown
warehouse SKU. `sku` is the exact matching key for v1. A missing value must not overwrite a store value. No fuzzy match
or currency conversion is needed for these cases.

## Development checkpoints

1. Upload and persist all three incoming files with stable names or IDs and unchanged source bytes.
2. Make each prompt resolve its `reads` and `writes` as recorded in `prompts.json`; ask on P19.
3. Compile read-only comparisons and updates/inserts against those exact tables. Aggregate repeated warehouse or
   competitor rows before using them as one value per SKU.
4. Preview proposed changes without committing them, then require approval; P20 must not delete on the first request.
5. Run each case against a fresh copy of the store. Assert its `expected` result and that all incoming bytes are unchanged.
