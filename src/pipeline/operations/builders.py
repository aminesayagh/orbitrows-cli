"""The registered builders: an enabled catalog entry names one (builder_key), never code. Each takes the entry's
resolved parameters and its implementation config and returns our AST (sql/dml.py): a condition (role condition) or a
value expression (role value). Columns are column nodes ({"type": "column", "source": table id, "reference"}); a
subquery's own alias is not a table id (assembly rewrites only those), as conditions.through() does.

    p   {"column", "other"?: column nodes, "operator"?: "<" | "<=" | ">" | ">=" | "=" | "!=", numbers, "value"?: node,
         "linked"?: {"table", "key": its column id pointing to `column`'s row}, "column_type"?: "TEXT" | "INTEGER" | "REAL"}
"""


def lit(v) -> dict:
    return {"type": "literal", "value": v}


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


def margin_percent(p, config):
    """a margin over 40%: (column − other) / column × 100 OP percent (a column of 0 or NULL: no margin, no row)."""
    margin = arith("*", arith("/", arith("-", p["column"], p["other"]), p["column"]), lit(100))
    return compare(p["operator"], margin, lit(p["percent"]))


def table_aggregate(p, config):
    """priced above the average: column OP (SELECT AVG(column) FROM its table); config {"function": avg | min | max}."""
    table, alias = p["column"]["source"], f"{p['column']['source']}_all"
    inner = {"type": "column", "source": alias, "reference": p["column"]["reference"]}
    return compare(p["operator"], p["column"],
                   one_value(table, alias, {"type": "aggregate", "function": config["function"], "argument": inner}))


def count_linked(p, config):
    """products with more than 3 variants: (SELECT COUNT(*) FROM linked WHERE linked.key = column) OP number."""
    alias = f"{p['linked']['table']}_n"
    where = compare("=", {"type": "column", "source": alias, "reference": p["linked"]["key"]}, p["column"])
    count = one_value(p["linked"]["table"], alias, {"type": "aggregate", "function": "count"}, where)
    return compare(p["operator"], count, lit(p["number"]))


def within_days(p, config):
    """sold in the last 30 days (config {"direction": "within"}): column >= date('now', '-30 days'); older than 30
    days ("older"): column < that date."""
    since = func("date", lit("now"), lit(f"-{int(p['days'])} days"))
    return compare(">=" if config["direction"] == "within" else "<", p["column"], since)


# ---- values: they compute a new value ------------------------------------------------------------------------------

def percent_change(p, config):
    """raise by 15% (config {"sign": 1}), lower by 15% (-1): column × (1 ± percent / 100)."""
    return arith("*", p["column"], lit(1 + config["sign"] * p["percent"] / 100))


def clamp(p, config):
    """cap at 500 (config {"bound": "max"}): MIN(column, 500); at least 500 ("min"): MAX(column, 500). A NULL value
    stays NULL (SQLite's MIN / MAX of a NULL is NULL)."""
    return func("least" if config["bound"] == "max" else "greatest", p["column"], lit(p["limit"]))


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


BUILDERS = {f.__name__: f for f in (percent_of_column, multiple_of_column, margin_percent, table_aggregate,
                                    count_linked, within_days, percent_change, clamp, set_value, fill_empty)}
