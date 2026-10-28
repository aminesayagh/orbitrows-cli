"""Every operation delivered is found by the search (operations/index.py) for requests written apart from its examples:
among the entries of its role, in the top K a step shows Jev. Loads the search model (bge-small): its own suite,
`uv run python -m pytest -m catalog` (the default run leaves it out)."""
import pytest

from orbitrows.pipeline.operations.catalog import connect, entries
from orbitrows.pipeline.operations.index import Index

pytestmark = pytest.mark.catalog
K = 10                                                        # what step 6 shows Jev (conditions.catalog_candidates)
HELD_OUT = {   # entry → requests never among its examples: other wording, other tables
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
