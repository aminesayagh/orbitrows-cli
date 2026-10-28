"""Every operation delivered is found by the search (operations/index.py) for requests written apart from its examples:
among the entries of its role, in the top K a step shows Jev. Loads the search model (bge-small): its own suite,
`uv run python -m pytest -m catalog` (the default run leaves it out)."""
import pytest

from orbitrows.pipeline.operations.catalog import connect, entries
from orbitrows.pipeline.operations.index import Index

pytestmark = pytest.mark.catalog
K = 10                                                        # what a step shows Jev (operations/use.py's search)
HELD_OUT = {   # entry → requests never among its examples: other wording, other tables
    "TOP_PER_GROUP": ["Show the cheapest flight of every route", "Keep each author's three newest articles",
                      "Which are the highest scoring players in each team?"],
    "NTH_PER_GROUP": ["Find the second highest salary in every department",
                      "Show each customer's second purchase", "The fourth fastest lap of each driver"],
    "COMPARE_PREVIOUS": ["Show the days when the temperature rose compared with the day before",
                         "Which invoices are larger than the customer's previous invoice?",
                         "Find the weeks where sign-ups fell from the prior week"],
    "RANK_VALUES": ["Give every city a rank by population", "Show each athlete's standing by time within their event",
                    "Which position does each branch hold by total sales?"],
    "PERCENTILE_RANK": ["Show how each house price compares as a percentile of all prices",
                        "In which percentile does each runner's time fall within their age group?",
                        "Express every exam mark as a percentile rank"],
    "PREVIOUS_VALUE": ["Next to each payment, show the amount of the payment before it",
                       "Show each meter reading alongside the preceding reading",
                       "For every login, the date of the user's prior login"],
    "NEXT_VALUE": ["Beside each invoice, show the date of the invoice that follows",
                   "Show each step together with the step after it",
                   "For every flight, the departure time of the subsequent flight on the route"],
    "RUNNING_TOTAL": ["Show the total so far of deposits by date", "Accumulate the hours worked day by day per employee",
                      "A cumulative sum of units shipped over the year"],
    "FIRST_IN_GROUP": ["Beside each sale, show the date of that customer's earliest sale",
                       "Show every reading with the first reading of its sensor",
                       "Each student's initial score next to all their scores"],
    "LAST_IN_GROUP": ["Beside each row, show the most recent price of that product",
                      "Show each shipment with the final status of its order",
                      "Every log line next to the last message of its session"],
    "COMPARE_TEXT_LENGTH": ["Show the customers whose email address has fewer than 12 characters",
                            "Which article headlines run past 80 characters?",
                            "Find the SKUs that are longer than ten characters"],
    "COMPARE_ABSOLUTE_DIFFERENCE": ["Show the products whose list price and sale price are more than 20 apart",
                                    "Which readings differ from the target by over 0.5, up or down?",
                                    "Find rows where forecast and actual are within 5 units of each other"],
    "COMPARE_DAYS_BETWEEN": ["Show the shipments that arrived more than a week after dispatch",
                             "Which loans were returned within 14 days of being issued?",
                             "Find the subscriptions cancelled less than 30 days after they started"],
    "MULTIPLE_OF_NUMBER": ["Show the invoices whose number is divisible by 4",
                           "Keep every 20th customer by their id",
                           "Which batch sizes are a multiple of 25?"],
    "EVEN_VALUES": ["Show only the lockers with an even number",
                    "Which tables have an even table number?",
                    "List the even-numbered rows of the import"],
    "ODD_VALUES": ["Show only the lockers with an odd number",
                   "Which houses are on the odd side of the street, by house number?",
                   "List the odd-numbered invoices"],
    "LIST_VALUES": ["For each order, show the products it contains in one cell",
                    "Show every author of each book, joined together",
                    "Which ports does each ship call at? One line per ship"],
    "ROUND_UP": ["Round the delivery fees up to whole euros",
                 "Bring each package weight up to the next whole kilogram",
                 "Every partial hour should count as a full one: round the hours up"],
    "ROUND_DOWN": ["Round the ages down to whole years",
                   "Bring the scores down to the whole number below",
                   "Round every balance down"],
    "DROP_DECIMALS": ["Strip the decimal part off the temperatures",
                      "Keep just the whole number of each measurement, ignoring what follows the point",
                      "Truncate the exchange rates"],
    "ABSOLUTE_VALUE": ["Make all the balance adjustments positive",
                       "Remove the sign from the temperature changes",
                       "Turn the negative refunds into positive amounts"],
    "SQUARE_ROOT": ["Replace the variance by its square root",
                    "Compute the root of each surface value",
                    "Store the square root of the scores instead"],
    "RAISE_TO_POWER": ["Square the distances",
                       "Cube every volume factor",
                       "Raise each growth rate to the power of 4"],
}


@pytest.fixture(scope="module")
def search():
    db = connect()
    yield Index(db), {e["id"]: e for e in entries(db)}
    db.close()


def test_every_delivered_operation_has_its_held_out_requests(search):
    _, by_id = search
    assert set(HELD_OUT) <= set(by_id) and all(len(rs) >= 3 for rs in HELD_OUT.values())


@pytest.mark.parametrize("entry_id, asked", [(e, r) for e, rs in HELD_OUT.items() for r in rs])
def test_an_operation_is_found_for_a_request_of_its_own(search, entry_id, asked):
    index, by_id = search
    role = by_id[entry_id]["role"]
    ranked = [i for i, _ in index.retrieve(asked, len(by_id)) if by_id[i]["role"] == role]
    assert entry_id in ranked[:K], (asked, ranked[:5])


def test_the_shared_search_keeps_the_role_and_the_column_s_kind():
    from orbitrows.pipeline.operations import use
    found = use.search("raise the prices by 10%", "value", "number")
    assert "INCREASE_PERCENT" in found and len(found) == use.K           # among what Jev chooses from
    assert all(use.applies(use.catalog()["entries"][i], "text") for i in use.search("write the names in capitals",
                                                                                     "value", "text"))


# 2026-10-28, step 1.2: every value change of the prompt sets (by hand, from their expected SQL), the operations the
# formula will search for, part by part (words.segments, as step 1.3 searches)
HALF = ("MULTIPLY_BY", "DIVIDE_BY", "DECREASE_PERCENT")      # "by half": × 0.5, ÷ 2 or − 50%
VALUE_CHANGES = [   # (request, the changed column's kind, the operations it needs: any of a tuple is right)
    ('Archive the Casa Living products.', 'text', ['SET_VALUE']),
    ('Lower the price of the Desk Lamp by 5%.', 'number', ['DECREASE_PERCENT']),
    ('Raise the lead time of Tech Source by 2 days.', 'number', ['ADD_AMOUNT']),
    ('Lower the price of the VOLT products by 10%.', 'number', ['DECREASE_PERCENT']),
    ('Set the price of the Wireles Mouse to 170.', 'number', ['SET_VALUE']),
    ('Archive the PaperHub stationery.', 'text', ['SET_VALUE']),
    ('Set the status of the products to active, but archived for the Orbit products.', 'text', ['SET_VALUE']),
    ('Set the sale date of the refunded sales to 1 November 2026.', 'date', ['SET_VALUE']),
    ('Move the sale date of the pending sales 7 days later.', 'date', ['SHIFT_DATE']),
    ('Raise the price of the black variants by 5%, then show the prices of those.', 'number', ['INCREASE_PERCENT']),
    ('Show the names of those, but first lower the price of the black variants by 5%.', 'number', ['DECREASE_PERCENT']),
    ('Raise the price of the black variants heavier than 500 g by 5%.', 'number', ['INCREASE_PERCENT']),
    ('Lower the price of the Volt variants heavier than 60 g by 60%, but never below their cost, then show the names and price.', 'number', ['DECREASE_PERCENT', 'CLAMP_MIN']),
    ('Show the price minus the cost of every variant.', 'number', ['SUBTRACT_AMOUNT']),
    ('Show the prices of the variants with 20% tax added.', 'number', ['INCREASE_PERCENT']),
    ('Show the price of the black variants after a 10% discount.', 'number', ['DECREASE_PERCENT']),
    ("Show each variant's price rounded to the nearest 50.", 'number', ['ROUND_TO_NEAREST']),
    ('Show the price of the variants, never above 200.', 'number', ['CLAMP_MAX']),
    ('Show the 3 variants with the largest difference between price and cost.', 'number', ['SUBTRACT_AMOUNT']),
    ('List the variants sorted by price minus cost, smallest first.', 'number', ['SUBTRACT_AMOUNT']),
    ('Show the 2 sales with the highest quantity times unit price.', 'number', ['MULTIPLY_BY']),
    ('Raise the prices of the Orbit variants by 15%, then take 5 off, but for the black ones apply the 15% only after removing the 5. Round everything to the nearest 5, and never go above twice their cost or below the cost plus 10%. Leave the variants without a cost unchanged.', 'number', ['INCREASE_PERCENT', 'SUBTRACT_AMOUNT', 'ROUND_TO_NEAREST', 'CLAMP_MAX', 'CLAMP_MIN']),
    ('Lower the price of the Casa variants by 10%, then round it to the nearest 10.', 'number', ['DECREASE_PERCENT', 'ROUND_TO_NEAREST']),
    ('Divide the weight of the Volt variants by 2, but never below 40 g.', 'number', ['DIVIDE_BY', 'CLAMP_MIN']),
    ('Double the prices of the Casa variants.', 'number', ['MULTIPLY_BY']),
    ('Reduce the price of the Casa variants by half.', 'number', [HALF]),
    ('Set the price of the Volt variants to a third of their cost.', 'number', ['SET_FROM_COLUMN', ('MULTIPLY_BY', 'DIVIDE_BY')]),
    ('Increase the price of the black variants by 15%.', 'number', ['INCREASE_PERCENT']),
    ('Decrease the price of the black variants by 15%.', 'number', ['DECREASE_PERCENT']),
    ('Set the price of the black variants to 15% of their cost.', 'number', ['SET_FROM_COLUMN', 'MULTIPLY_BY']),
    ('Lower the price of the white variants by ten percent.', 'number', ['DECREASE_PERCENT']),
    ('Set the price of the Casa variants to two and a half times their cost.', 'number', ['SET_FROM_COLUMN', 'MULTIPLY_BY']),
    ("Add ' (old)' at the end of the names of the archived products.", 'text', ['ADD_SUFFIX']),
    ("In the product descriptions, replace 'cotton' with 'organic cotton'.", 'text', ['REPLACE_TEXT']),
    ('Write the brands of the Volt products in capitals.', 'text', ['UPPER_CASE']),
    ('Put the colours of the variants in lower case.', 'text', ['LOWER_CASE']),
    ('Remove the spaces around the product descriptions.', 'text', ['TRIM_SPACES']),
    ('Clear the barcode of the variants heavier than 1 kg.', 'text', ['CLEAR_VALUE']),
    ("Fill the missing colors of the variants with 'black'.", 'text', ['FILL_EMPTY']),
    ("Add 'NEW-' in front of the SKUs of the Casa variants.", 'text', ['ADD_PREFIX']),
    ('Set the status of the draft products to active.', 'text', ['SET_VALUE']),
    ('Set the description of the Gift Card to its name.', 'text', ['SET_FROM_COLUMN']),
    ('Move the sale dates of the pending sales 2 weeks earlier.', 'date', ['SHIFT_DATE_EARLIER']),
    ('Remove the word Kids from the product names.', 'text', ['REMOVE_TEXT']),
    ('In the product names, replace Orbit with ORBIT, but with Orbit+ for the active products.', 'text', ['REPLACE_TEXT']),
    ("Write the product names in lower case, trim them, and add 'x-' at the start.", 'text', ['LOWER_CASE', 'TRIM_SPACES', 'ADD_PREFIX']),
    ("In the descriptions, replace 'cotton' with 'linen', then write them in capitals.", 'text', ['REPLACE_TEXT', 'UPPER_CASE']),
    ('Push the sold_on date of the pending sales back by 2 weeks.', 'date', ['SHIFT_DATE']),
    ('Set the sold_on date of the refunded sales to October 1, 2026.', 'date', ['SET_VALUE']),
    ('Move the created_at date of the Orbit products one month earlier.', 'date', ['SHIFT_DATE_EARLIER']),
    ('Set the sold_on date of the cancelled sales to yesterday.', 'date', ['SET_VALUE']),
    ('Move the sold_on date of the pending sales a fortnight later.', 'date', ['SHIFT_DATE']),
    ('Divide the prices of the black variants by 7, then round them up.', 'number', ['DIVIDE_BY', 'ROUND_UP']),
    ('Divide the prices of the black variants by 7, then round them down.', 'number', ['DIVIDE_BY', 'ROUND_DOWN']),
    ('Divide the weights of the navy variants by 7 and drop the decimals.', 'number', ['DIVIDE_BY', 'DROP_DECIMALS']),
    ('Lower the costs of the black variants by 500, then make them positive.', 'number', ['SUBTRACT_AMOUNT', 'ABSOLUTE_VALUE']),
    ('Replace the prices of the black variants by their square root.', 'number', ['SQUARE_ROOT']),
    ('Raise the prices of the navy variants to the power of 2.', 'number', ['RAISE_TO_POWER']),
]
FOUND_FLOOR = 74                                               # of 75, in the top 10 (measured 2026-10-28, better examples)


def test_the_value_changes_operations_are_found_part_by_part():
    """A floor, not a goal: a change to the catalog or its examples that loses one of these fails. Below it today:
    INCREASE_PERCENT for "with 20% tax added" (rank 11: the table and column words outweigh "tax added")."""
    from orbitrows.pipeline.expressions.words import segments
    from orbitrows.pipeline.operations import use
    n, found, missed = len(use.catalog()["entries"]), 0, []
    for text, kind, ops in VALUE_CHANGES:
        parts = [use.search(text[a:b], "value", kind, n) for a, b in segments(text)] + [
            use.search(text, "value", kind, n)]
        for op in ops:
            ok = any(any(o in p[:K] for p in parts) for o in (op if isinstance(op, tuple) else (op,)))
            found += ok
            missed += [] if ok else [(text, op)]
    assert found >= FOUND_FLOOR, (found, missed)
