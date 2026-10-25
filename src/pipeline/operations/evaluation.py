"""Phase 3's measure: held-out requests (never the catalog's own examples), each with the operation phrase a
request step would extract, and the entry it means (None: no entry supports it, it must be refused later).
In English, as the CLI's client writes them (an LLM driving it), several kinds of tables, paraphrases and goals. Run by the developer (it loads the models):

    uv run python -m orbitrows.pipeline.operations.evaluation [config]   Recall@K per input (the default model)
"""
CASES = [   # (whole request, extracted phrase, entry or None)
    ("Show the variants whose cost is below a third of the price", "cost below a third of the price",
     "COMPARE_PERCENT_OF_COLUMN"),
    ("Which listings sell for over 4 times what they cost us?", "sell for over 4 times what they cost",
     "COMPARE_MULTIPLE_OF_COLUMN"),
    ("Find the products where we keep less than 25% of the selling price after the cost",
     "keep less than 25% of the selling price after the cost", "COMPARE_SHARE_OF_FIRST"),
    ("Show the departments that have under 5% of their budget left after spending",
     "under 5% of their budget left after spending", "COMPARE_SHARE_OF_FIRST"),
    ("Which items carry a markup above 60% on their cost?", "a markup above 60% on their cost",
     "COMPARE_SHARE_OF_SECOND"),
    ("Show the variants that sell for at least 80 more than they cost", "sell for at least 80 more than they cost",
     "COMPARE_DIFFERENCE"),
    ("Which flights landed more than 30 minutes after the scheduled time?",
     "landed more than 30 minutes after the scheduled time", "COMPARE_DIFFERENCE"),
    ("List the products that weigh more than the average", "weigh more than the average", "COMPARE_TABLE_AVERAGE"),
    ("Show me the cheapest variant price records, every one at that lowest price", "at that lowest price",
     "COMPARE_TABLE_MINIMUM"),
    ("Who got the top score of the whole exam?", "the top score of the whole exam", "COMPARE_TABLE_MAXIMUM"),
    ("Which suppliers provide more than five products?", "provide more than five products", "COUNT_LINKED_RECORDS"),
    ("Show the variants with fewer than 20 units in stock overall", "fewer than 20 units in stock overall",
     "SUM_LINKED_RECORDS"),
    ("Show the sales made this past month", "made this past month", "WITHIN_LAST_DAYS"),
    ("Delete the logs older than 180 days", "older than 180 days", "OLDER_THAN_DAYS"),
    ("Bump the prices up by 6 percent", "up by 6 percent", "INCREASE_PERCENT"),
    ("Give a 20% discount on the winter items", "a 20% discount", "DECREASE_PERCENT"),
    ("Add 15 to every order quantity", "add 15", "ADD_AMOUNT"),
    ("Knock 10 off the price of the clearance items", "knock 10 off", "SUBTRACT_AMOUNT"),
    ("Triple the loyalty points", "triple", "MULTIPLY_BY"),
    ("Make the sale price the purchase price plus 30", "the purchase price plus 30", "SET_FROM_COLUMN"),
    ("Show the prices with no more than two decimals", "no more than two decimals", "ROUND_DECIMALS"),
    ("Round the prices to the closest 10", "to the closest 10", "ROUND_TO_NEAREST"),
    ("Make sure no price is higher than 999", "no price is higher than 999", "CLAMP_MAX"),
    ("Never let the stock fall under 2", "never fall under 2", "CLAMP_MIN"),
    ("Set every status to archived", "set to archived", "SET_VALUE"),
    ("Where there's no email, write none@example.com", "where there's no email, write", "FILL_EMPTY"),
    ("Put 'OLD-' in front of every reference", "in front of every reference", "ADD_PREFIX"),
    ("Append ' - discontinued' to the product names", "append to the names", "ADD_SUFFIX"),
    ("Change every 'Ltd' into 'Limited' in the company names", "change every 'Ltd' into 'Limited'", "REPLACE_TEXT"),
    ("Make the city names all caps", "all caps", "UPPER_CASE"),
    ("Convert the emails to lowercase", "to lowercase", "LOWER_CASE"),
    ("Strip the leading and trailing blanks from the codes", "strip the leading and trailing blanks",
     "TRIM_SPACES"),
    ("Push the delivery dates back by 3 days", "push back by 3 days", "SHIFT_DATE"),
    ("Wipe the phone numbers of the deleted accounts", "wipe the phone numbers", "CLEAR_VALUE"),
    # as an LLM agent driving the CLI writes them: explicit tables and columns, sometimes SQL-like words
    ("In the variants table, update price_mad to price_mad + 12 for every row", "update price_mad to price_mad + 12",
     "ADD_AMOUNT"),
    ("Prepend the string 'ARCH-' to the sku column of the archived products", "prepend the string 'ARCH-'",
     "ADD_PREFIX"),
    ("Concatenate ' (legacy)' to the end of the name column for the old suppliers",
     "concatenate ' (legacy)' to the end", "ADD_SUFFIX"),
    ("Update the variants so that price_mad never exceeds 750", "price_mad never exceeds 750", "CLAMP_MAX"),
    ("Set a floor of 10 on the reorder_point column", "a floor of 10", "CLAMP_MIN"),
    ("Set the barcode column to NULL for variants without a product", "set the barcode column to NULL",
     "CLEAR_VALUE"),
    ("Select the variants where price_mad is greater than 2.5 * cost_mad", "price_mad greater than 2.5 * cost_mad",
     "COMPARE_MULTIPLE_OF_COLUMN"),
    ("Return the rows where discount_mad exceeds 25 percent of unit_price_mad",
     "discount_mad exceeds 25 percent of unit_price_mad", "COMPARE_PERCENT_OF_COLUMN"),
    ("List rows where (price_mad - cost_mad) / cost_mad is above 0.8, as a markup",
     "(price_mad - cost_mad) / cost_mad above 0.8", "COMPARE_SHARE_OF_SECOND"),
    ("Return the sales whose quantity is below the mean quantity of the sales table", "below the mean quantity",
     "COMPARE_TABLE_AVERAGE"),
    ("Select the supplier rows with the maximum lead_days value", "with the maximum lead_days value",
     "COMPARE_TABLE_MAXIMUM"),
    ("Return the inventory rows whose quantity equals the minimum quantity in the table",
     "quantity equals the minimum quantity", "COMPARE_TABLE_MINIMUM"),
    ("List the categories referenced by more than 2 rows of the products table", "referenced by more than 2 rows",
     "COUNT_LINKED_RECORDS"),
    ("Reduce unit_price_mad by 7.5% on all pending sales", "reduce by 7.5%", "DECREASE_PERCENT"),
    ("Replace NULL or blank values in the size column with 'one-size'", "replace NULL or blank values with",
     "FILL_EMPTY"),
    ("Apply a 4% increase to the lead_days column", "apply a 4% increase", "INCREASE_PERCENT"),
    ("Normalize the contact_email column to lower case", "normalize to lower case", "LOWER_CASE"),
    ("Scale the weight_g column by a factor of 1000", "scale by a factor of 1000", "MULTIPLY_BY"),
    ("Return the sales with sold_on earlier than 60 days before today", "earlier than 60 days before today",
     "OLDER_THAN_DAYS"),
    ("Substitute 'Volt Inc' with 'Volt' in the brand column", "substitute 'Volt Inc' with 'Volt'", "REPLACE_TEXT"),
    ("Round cost_mad to 1 decimal place", "round to 1 decimal place", "ROUND_DECIMALS"),
    ("Round price_mad to a multiple of 25", "round to a multiple of 25", "ROUND_TO_NEAREST"),
    ("Set cost_mad equal to price_mad minus 40 for the clearance variants", "set cost_mad equal to price_mad minus 40",
     "SET_FROM_COLUMN"),
    ("Update the status column to 'draft' for the Casa products", "update the status column to 'draft'",
     "SET_VALUE"),
    ("Add 14 days to the sold_on date of the pending sales", "add 14 days to the sold_on date", "SHIFT_DATE"),
    ("Subtract 1 from the reserved column in Rabat", "subtract 1 from the reserved column", "SUBTRACT_AMOUNT"),
    ("Return the products whose variants' quantity in inventory sums to more than 100", "sums to more than 100",
     "SUM_LINKED_RECORDS"),
    ("Remove leading and trailing whitespace from the description column", "remove leading and trailing whitespace",
     "TRIM_SPACES"),
    ("Convert the warehouse column to upper case", "convert to upper case", "UPPER_CASE"),
    ("Return the products whose created_at falls within the last 45 days", "within the last 45 days",
     "WITHIN_LAST_DAYS"),
    # no entry supports these: they must be refused, never matched to a close one
    ("Show the variants priced above the median", "above the median", None),
    ("Find the 10% most expensive products", "the 10% most expensive", None),
    ("Which salaries are more than two standard deviations from the mean?", "two standard deviations from the mean",
     None),
    ("Show the 90th percentile of delivery times", "90th percentile", None),
    ("Rank the stores by revenue", "rank by revenue", None),
    ("Remove the duplicate customers", "remove the duplicates", None),
    ("Split the full name into first and last name", "split into first and last name", None),
    ("Convert the prices from dollars to euros at today's rate", "convert at today's rate", None),
    ("Translate the descriptions into Spanish", "translate into Spanish", None),
    ("Fix the spelling mistakes in the titles", "fix the spelling mistakes", None),
    ("Show the running total of sales by day", "running total by day", None),
    ("Show the sales from last quarter", "from last quarter", None),
    ("Encrypt the passwords", "encrypt", None),
    ("Predict next month's sales", "predict next month", None),
    ("Return the variants whose price_mad is above the 75th percentile", "above the 75th percentile", None),
    ("Compute the standard deviation of price_mad per brand", "standard deviation per brand", None),
]


def measure(index, field: int) -> dict:
    """Recall@1/3/5/10 on the supported cases, and the best similarity of each refused one next to the right
    entry's similarity on the supported ones (the gap a rejection threshold would need)."""
    hits, right, refused = {1: 0, 3: 0, 5: 0, 10: 0}, [], []
    supported = [c for c in CASES if c[2]]
    for c in CASES:
        found = index.retrieve(c[field], 10)
        if c[2] is None:
            refused.append(found[0][1])
            continue
        ranks = [i for i, (op, _) in enumerate(found) if op == c[2]]
        for k in hits:
            hits[k] += bool(ranks and ranks[0] < k)
        right.append(next((s for op, s in found if op == c[2]), 0.0))
    return {f"R@{k}": round(v / len(supported), 2) for k, v in hits.items()} | {
        "right median": round(sorted(right)[len(right) // 2], 2), "refused max": round(max(refused), 2),
        "refused median": round(sorted(refused)[len(refused) // 2], 2)}


if __name__ == "__main__":
    import sys
    from .catalog import PATH, connect
    from .index import DEFAULT, Index, embed
    db = connect(PATH, write=True)
    for config_id in sys.argv[1:] or [DEFAULT]:              # another model: measured, and its vectors stored
        print(config_id, "entries embedded:", embed(db, config_id))
        index = Index(db, config_id)
        print("stale:", index.stale)
        for field, name in ((0, "request"), (1, "phrase")):
            print(f"{config_id:11} {name:8}", measure(index, field))
