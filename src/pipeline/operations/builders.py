"""The registered builders: an enabled catalog entry names one (builder_key), never code. Each takes the entry's
resolved parameters and its implementation config and returns our AST (sql/dml.py): a condition (role condition) or a
value expression (role value). Columns are column nodes ({"type": "column", "source": table id, "reference"}); a
subquery's own alias is not a table id (assembly rewrites only those), as conditions.through() does.

    p   {"column", "other"?: column nodes, "operator"?: "<" | "<=" | ">" | ">=" | "=" | "!=", numbers, "value"?: node,
         "linked"?: {"table", "key": its column id pointing to `column`'s row}, "column_type"?: "TEXT" | "INTEGER" | "REAL"}
"""


def lit(v) -> dict:
    return {"type": "literal", "value": v}


def given(x) -> dict:
    """A parameter as a node: a number or a text given as is, or already a node (a column, another step's result:
    the formula composes them)."""
    return x if isinstance(x, dict) else lit(x)


def arith(op, left, right) -> dict:
    return {"type": "arithmetic", "operator": op, "left": left, "right": right}


def compare(op, left, right) -> dict:
    return {"type": "comparison", "operator": op, "left": left, "right": right}


def func(name, *args) -> dict:
    return {"type": "function", "name": name, "arguments": list(args)}


def one_value(table, alias, expression, where=None) -> dict:
    """(SELECT expression FROM table AS alias [WHERE …]): one row, an aggregate without GROUP BY."""
    return {"type": "subquery", "query": {
        "id": alias, "type": "select", "from": {"type": "database_table", "reference": table, "alias": alias},
        "columns": [{"expression": expression, "alias": "v"}],
        "scope": {"type": "filtered", "where": where} if where else {"type": "all_rows"}}}


# ---- conditions: they choose rows ----------------------------------------------------------------------------------

def percent_of_column(p, config):
    """cost < 10% of the price: column OP other × percent / 100."""
    return compare(p["operator"], p["column"], arith("*", p["other"], lit(p["percent"] / 100)))


def multiple_of_column(p, config):
    """price > twice the cost: column OP other × factor."""
    return compare(p["operator"], p["column"], arith("*", p["other"], lit(p["factor"])))


def relative_difference(p, config):
    """the difference as a share of one value (config {"base": "column"}: (column − other) / column × 100, a margin
    on a price; "other": (column − other) / other × 100, a markup on a cost, a growth) OP percent. A base of 0 or NULL
    has no share: no row."""
    base = p["column"] if config["base"] == "column" else p["other"]
    share = arith("*", arith("/", arith("-", p["column"], p["other"]), base), lit(100))
    return compare(p["operator"], share, lit(p["percent"]))


def difference(p, config):
    """at least 50 between the two: (column − other) OP amount."""
    return compare(p["operator"], arith("-", p["column"], p["other"]), lit(p["amount"]))


def table_aggregate(p, config):
    """priced above the average: column OP (SELECT AVG(column) FROM its table); config {"function": avg | min | max}."""
    table, alias = p["column"]["source"], f"{p['column']['source']}_all"
    inner = {"type": "column", "source": alias, "reference": p["column"]["reference"]}
    return compare(p["operator"], p["column"],
                   one_value(table, alias, {"type": "aggregate", "function": config["function"], "argument": inner}))


AGGREGATES = ("count", "count_distinct", "sum", "avg", "min", "max", "all")


def inside(cond, table, alias):
    """A condition on the linked table's columns, read inside its subquery (the table's columns → the alias's)."""
    import json
    return json.loads(json.dumps(cond).replace(f'"source": "{table}"', f'"source": "{alias}"'))


def columns_in(node) -> list:
    if isinstance(node, dict):
        return [node] if node.get("type") == "column" else [c for v in node.values() for c in columns_in(v)]
    return [c for v in node for c in columns_in(v)] if isinstance(node, list) else []


def linked_aggregate(p, config):
    """products with more than 3 variants, customers whose orders total over 1000, products whose cheapest variant is
    under 100: (SELECT AGG(linked.value) FROM linked WHERE linked.key = column [AND where]) OP number, AGG one of
    AGGREGATES (p["function"]; count needs no value column); p["where"]: a condition on the linked records, inside
    ("more than 2 black variants": only the black ones are counted). Over no linked record: a count is 0, a total 0
    (COALESCE: "stock under 10" keeps the variant with no stock line), an average, lowest or highest has no value: not
    kept. p["linked"]["through"] ({"table", "key": its column pointing to `column`'s row, "id": its column the
    linked table points to}): the linked records are two links away, through that middle table. "all" (every linked
    record meets p["where"]): at least one linked record, and none that breaks it, a record
    whose value is empty breaking it too (never silently left out: NOT (NULL = x) is not true)."""
    f, table = p["function"], p["linked"]["table"]
    alias = f"{table}_agg"
    key = compare("=", {"type": "column", "source": alias, "reference": p["linked"]["key"]}, p["column"])
    if via := p["linked"].get("through"):                     # two links away (products → variants → inventory):
        m = f"{via['table']}_via"                             # its key among the middle records of this one
        key = {"type": "membership", "operator": "in", "expression": key["left"], "query": {
            "id": m, "type": "select", "from": {"type": "database_table", "reference": via["table"], "alias": m},
            "columns": [{"expression": {"type": "column", "source": m, "reference": via["id"]}, "alias": "k"}],
            "scope": {"type": "filtered", "where": compare("=", {"type": "column", "source": m,
                                                                "reference": via["key"]}, p["column"])}}}
    where = inside(p["where"], table, alias) if p.get("where") else None

    def count(extra=None):
        w = {"type": "all", "children": [key, extra]} if extra else key
        return one_value(table, alias, {"type": "aggregate", "function": "count"}, w)
    if f == "all":
        empty = [{"type": "null_check", "expression": c, "operator": "is_null"} for c in columns_in(where)]
        breaks = {"type": "any", "children": [{"type": "not", "operand": where}, *empty]} if empty else \
            {"type": "not", "operand": where}
        return {"type": "all", "children": [compare(">", count(), lit(0)), compare("=", count(breaks), lit(0))]}
    value = {"type": "column", "source": alias, "reference": p["linked"].get("value")} if f != "count" else None
    agg = {"type": "aggregate", "function": "count" if f == "count_distinct" else f, "distinct": f == "count_distinct"}
    if value:
        agg["argument"] = value
    total = one_value(table, alias, agg, {"type": "all", "children": [key, where]} if where else key)
    if f == "sum":
        total = func("coalesce", total, lit(0))
    return compare(p["operator"], total, given(p["number"]))


def length_compare(p, config):
    """names longer than 20 characters: LENGTH(column) OP number."""
    return compare(p["operator"], func("length", p["column"]), given(p["number"]))


def absolute_difference(p, config):
    """price and cost more than 50 apart, either way: ABS(column − other) OP amount."""
    return compare(p["operator"], func("abs", arith("-", p["column"], p["other"])), given(p["amount"]))


def days_between(p, config):
    """sold more than 30 days after the product was created: JULIANDAY(column) − JULIANDAY(other) OP number (days,
    the later date first)."""
    return compare(p["operator"], arith("-", func("julianday", p["column"]), func("julianday", p["other"])),
                   given(p["number"]))


def remainder(p, config):
    """the even values (config {"divisor": 2, "remainder": 0}), the odd ones ({"divisor": 2, "remainder": 1}), the
    multiples of a number (no config: the request's number): column % divisor = remainder."""
    divisor = config.get("divisor") or p.get("divisor")
    return compare("=", arith("%", p["column"], given(divisor)), lit(config.get("remainder", 0)))


# ---- values: they compute a new value ------------------------------------------------------------------------------

def percent_change(p, config):
    """raise by 15% (config {"sign": 1}), lower by 15% (-1): column × (1 ± percent / 100)."""
    return arith("*", p["column"], lit(1 + config["sign"] * p["percent"] / 100))


def add_amount(p, config):
    """add 5 (config {"sign": 1}), take 5 off (-1): column ± amount."""
    return arith("+" if config["sign"] > 0 else "-", p["column"], given(p["amount"]))


def multiply(p, config):
    """double (factor 2), halve (0.5), triple: column × factor."""
    return arith("*", p["column"], given(p["factor"]))


def divide(p, config):
    """divide by 4: column ÷ divisor."""
    return arith("/", p["column"], given(p["divisor"]))


def share_of_column(p, config):
    """add 10% of the cost (config {"sign": 1}), take 10% of the cost off (-1): column ± other × percent / 100."""
    share = arith("*", p["other"], lit(p["percent"] / 100))
    return arith("+" if config["sign"] > 0 else "-", p["column"], share)


def from_column(p, config):
    """set the price to the cost plus 20: other (+ amount, when one is given)."""
    return arith("+", p["other"], lit(p["amount"])) if p.get("amount") else p["other"]


def function_of(p, config):
    """round up to a whole number (config {"function": "ceil"}), round down ("floor"), drop the decimals ("trunc"),
    the absolute value ("abs"), the square root ("sqrt"): FUNCTION(column)."""
    return func(config["function"], p["column"])


def power(p, config):
    """the value squared (exponent 2), cubed (3): POWER(column, exponent)."""
    return func("power", p["column"], given(p["exponent"]))


def round_digits(p, config):
    """round to 2 decimals: ROUND(column, digits); to whole numbers: ROUND(column)."""
    return func("round", p["column"], lit(int(p.get("digits") or 0)))


def round_to_step(p, config):
    """round to the nearest 5: ROUND(column / step) × step; the step divides as a decimal (123 / 5 is 24.6, never
    SQLite's whole 24)."""
    return arith("*", func("round", arith("/", p["column"], lit(float(p["step"]))), lit(0)), lit(p["step"]))


def clamp(p, config):
    """cap at 500 (config {"bound": "max"}): MIN(column, 500); at least 500 ("min"): MAX(column, 500). A NULL value
    stays NULL (SQLite's MIN / MAX of a NULL is NULL); a limit that can be empty (a column, another step's result:
    "never above twice the cost") keeps the value then: MIN(column, COALESCE(limit, column))."""
    limit = given(p["limit"])
    if limit["type"] != "literal":
        limit = func("coalesce", limit, p["column"])
    return func("least" if config["bound"] == "max" else "greatest", p["column"], limit)


def set_value(p, config):
    """set to 250: the value, whatever the row held."""
    return p["value"]


def fill_empty(p, config):
    """fill the missing values with 0: COALESCE(column, value); a text column's blank ('', spaces) is missing too:
    COALESCE(NULLIF(TRIM(column), ''), value)."""
    col = p["column"]
    if p.get("column_type") == "TEXT":
        col = func("nullif", func("trim", col), lit(""))
    return func("coalesce", col, p["value"])


def capitalize(p, config):
    """the first letter in capital, the rest as it is: UPPER(SUBSTR(column, 1, 1)) || SUBSTR(column, 2)."""
    return {"type": "concat", "parts": [func("upper", func("substr", p["column"], lit(1), lit(1))),
                                        func("substr", p["column"], lit(2))]}


def affix(p, config):
    """add 'NEW-' in front (config {"side": "start"}) or ' (old)' at the end ("end"): the text joined to the value.
    An empty value stays empty."""
    parts = [p["text"], p["column"]] if config["side"] == "start" else [p["column"], p["text"]]
    return {"type": "concat", "parts": parts}


def replace_text(p, config):
    """replace 'cotton' with 'organic cotton': REPLACE(column, old, new), every occurrence, case as written; remove
    the word Kids (no new text): REPLACE(column, old, '')."""
    return func("replace", p["column"], p["old"], p.get("new") or lit(""))


def text_function(p, config):
    """capitals (config {"function": "upper"}), lower case ("lower"), without the outer spaces ("trim")."""
    return func(config["function"], p["column"])


def shift_date(p, config):
    """move 7 days later: DATE(column, '+7 days'); 1 month earlier: DATE(column, '-1 months'); unit days, months or
    years (SQLite's modifiers)."""
    n = p["amount"] * config.get("sign", 1)                  # later (+), earlier (config {"sign": -1})
    return func("date", p["column"], lit(f"{'+' if n >= 0 else '-'}{abs(n)} {p['unit']}"))


def clear(p, config):
    """empty the barcode: no value (NULL)."""
    return lit(None)


# ---- outputs: what a read shows of a column ---------------------------------------------------------------------

def aggregate(p, config):
    """the column's values as they are (config {"function": "value"}), or one figure over the records it reads:
    COUNT, COUNT(DISTINCT), SUM, AVG, MIN, MAX (config {"function": "count", "distinct": true}…). Per group when the
    read is grouped ("per brand")."""
    if config["function"] == "value":
        return p["column"]
    return {"type": "aggregate", "function": config["function"], "argument": p["column"],
            "distinct": bool(config.get("distinct"))}


BUILDERS = {f.__name__: f for f in (
    percent_of_column, multiple_of_column, relative_difference, difference, table_aggregate, linked_aggregate,
    percent_change, add_amount, multiply, divide, share_of_column, from_column, round_digits, round_to_step, clamp,
    set_value, fill_empty, capitalize, affix, replace_text, text_function, shift_date, clear, aggregate,
    length_compare, absolute_difference, days_between, remainder, function_of, power)}
