"""The query AST nodes, declared as Pydantic models: the shape is checked by Pydantic; tables, columns and types by
SQLite (adapter in sql.py). Nodes follow standard SQL as SQLGlot names it (vault poc_v2/sql-ast-experimentation-guide.md
§4 for the base set): literal, column, arithmetic, concat (||), case, scalar functions, aggregates; comparison,
all/any/not, null check, membership (IN), between, pattern (LIKE … ESCAPE), empty check; select (GROUP BY, HAVING,
DISTINCT, ORDER BY, LIMIT), update, delete, insert (VALUES or a select); a select as one value (subquery) or as the list
of an IN. Unknown fields and node types are rejected;
an update or delete must state its scope (filtered, or all_rows).
"""
from typing import Annotated, Literal as L, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Node(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Lit(Node):
    type: L["literal"]
    value: str | int | float | bool | None


class Column(Node):
    type: L["column"]
    source: str
    reference: str


class Arithmetic(Node):
    type: L["arithmetic"]
    operator: L["+", "-", "*", "/", "%"]
    left: "Expression"
    right: "Expression"


class Negate(Node):
    type: L["negate"]
    operand: "Expression"


class Branch(Node):
    when: "Condition"
    then: "Expression"


class Case(Node):
    type: L["case"]
    branches: list[Branch] = Field(min_length=1)
    else_: "Expression" = Field(alias="else")


class DerivedReference(Node):
    type: L["derived_reference"]
    reference: str


# standard SQL scalar functions and their arity (min, max; None = any); greatest / least (SQLite MAX(a, b) / MIN(a, b))
# write a guard: "lower by 10% but never below cost + 10" = greatest(price * 0.9, cost + 10)
ARITY = {"trim": (1, 1), "length": (1, 1), "lower": (1, 1), "upper": (1, 1), "abs": (1, 1), "round": (1, 2),
         "replace": (3, 3), "coalesce": (2, None), "greatest": (2, None), "least": (2, None),
         "date": (2, 2), "substr": (2, 3)}                                      # date(col, '+7 days'): a date moved (2026-10-16)


class FunctionCall(Node):
    type: L["function"]
    name: L[tuple(ARITY)]
    arguments: list["Expression"] = Field(min_length=1)

    @model_validator(mode="after")
    def arity(self):
        lo, hi = ARITY[self.name]
        if len(self.arguments) < lo or (hi is not None and len(self.arguments) > hi):
            raise ValueError(f"INVALID_FUNCTION_ARITY {self.name}: {len(self.arguments)} arguments")
        return self


class Concat(Node):                                           # SQL a || b
    type: L["concat"]
    parts: list["Expression"] = Field(min_length=2)


class Aggregate(Node):                                        # COUNT(*), COUNT(DISTINCT x), SUM(x), …
    type: L["aggregate"]
    function: L["count", "sum", "avg", "min", "max"]
    argument: "Expression | None" = None
    distinct: bool = False

    @model_validator(mode="after")
    def star_is_count(self):
        if self.argument is None and (self.function != "count" or self.distinct):
            raise ValueError(f"{self.function} needs an argument (only COUNT(*) has none)")
        return self


class Subquery(Node):
    """(SELECT …) used as one value: "set the price to the average price of the tees". One column, and one row for
    sure: an aggregate without GROUP BY, or LIMIT 1 (SQLite would silently take the first of several rows)."""
    type: L["subquery"]
    query: "Select"

    @model_validator(mode="after")
    def one_value(self):
        q = self.query
        if len(q.columns) != 1:
            raise ValueError("a subquery used as a value returns one column")
        if not (q.limit == 1 or (not q.group_by and q.columns[0].expression.type == "aggregate")):
            raise ValueError("a subquery used as a value returns one row: an aggregate without GROUP BY, or LIMIT 1")
        return self


Expression = Annotated[Union[Lit, Column, Arithmetic, Negate, Case, DerivedReference, FunctionCall, Concat, Aggregate,
                             Subquery],
                       Field(discriminator="type")]


class Comparison(Node):
    type: L["comparison"]
    operator: L["=", "!=", "<", "<=", ">", ">="]
    left: Expression
    right: Expression


class All(Node):
    type: L["all"]
    children: list["Condition"] = Field(min_length=2)


class Any(Node):
    type: L["any"]
    children: list["Condition"] = Field(min_length=2)


class Not(Node):
    type: L["not"]
    operand: "Condition"


class NullCheck(Node):
    type: L["null_check"]
    expression: Expression
    operator: L["is_null", "is_not_null"]


class Membership(Node):
    """x IN (literals), or x IN (SELECT one column): a condition on another table."""
    type: L["membership"]
    expression: Expression
    values: list[Lit] | None = Field(None, min_length=1)
    query: "Select | None" = None
    operator: L["in", "not_in"]

    @model_validator(mode="after")
    def one_source(self):
        if (self.values is None) == (self.query is None):
            raise ValueError("a membership takes values or a query, exactly one")
        if self.query is not None and len(self.query.columns) != 1:
            raise ValueError("a membership query returns one column")
        return self


class Between(Node):
    type: L["between"]
    expression: Expression
    lower: Expression
    upper: Expression
    negated: bool


class Pattern(Node):
    type: L["pattern"]
    expression: Expression
    pattern: str
    operator: L["like", "not_like"]
    escape: str | None = Field(None, min_length=1, max_length=1)   # LIKE … ESCAPE '\': a literal % or _


class EmptyCheck(Node):
    type: L["empty_check"]
    expression: Expression
    operator: L["is_empty", "is_not_empty"]
    policy: L["null_or_space_only"]


Condition = Annotated[Union[Comparison, All, Any, Not, NullCheck, Membership, Between, Pattern, EmptyCheck],
                      Field(discriminator="type")]


class DatabaseTable(Node):
    type: L["database_table"]
    reference: str
    alias: str


class RequestResult(Node):
    type: L["request_result"]
    request_id: str
    alias: str


class Filtered(Node):
    type: L["filtered"]
    where: Condition


class AllRows(Node):
    type: L["all_rows"]


Source = Annotated[Union[DatabaseTable, RequestResult], Field(discriminator="type")]
Scope = Annotated[Union[Filtered, AllRows], Field(discriminator="type")]


class Assignment(Node):
    column: Column
    value: Expression


class OutputColumn(Node):
    expression: Expression
    alias: str


class OrderBy(Node):
    expression: Expression
    direction: L["asc", "desc"]


class Update(Node):
    id: str
    type: L["update"]
    target: DatabaseTable
    assignments: list[Assignment] = Field(min_length=1)
    scope: Scope


class Delete(Node):
    id: str
    type: L["delete"]
    target: DatabaseTable
    scope: Scope


class Select(Node):
    id: str
    type: L["select"]
    from_: Source = Field(alias="from")
    columns: list[OutputColumn] = Field(min_length=1)
    scope: Scope
    distinct: bool = False
    group_by: list[Expression] = []
    having: Condition | None = None
    order_by: list[OrderBy] = []
    limit: int | None = Field(None, ge=0)
    offset: int | None = Field(None, ge=0)


class Insert(Node):
    """INSERT INTO target (columns) VALUES (…), (…)  or  INSERT INTO target (columns) SELECT …"""
    id: str
    type: L["insert"]
    target: DatabaseTable
    columns: list[str] = Field(min_length=1)                  # column ids of the target
    values: list[list[Expression]] | None = None
    query: Select | None = None

    @model_validator(mode="after")
    def one_source(self):
        if (self.values is None) == (self.query is None):
            raise ValueError("an insert takes values or a query, exactly one")
        if self.values is not None and (not self.values or any(len(r) != len(self.columns) for r in self.values)):
            raise ValueError("each row of values has one value per column")
        if self.query is not None and len(self.query.columns) != len(self.columns):
            raise ValueError("the query returns one value per column")
        return self


Statement = Annotated[Union[Select, Update, Delete, Insert], Field(discriminator="type")]
for m in (Arithmetic, Negate, Branch, Case, FunctionCall, Concat, Aggregate, Subquery, All, Any, Not, Membership,
          Select, Insert):
    m.model_rebuild()
