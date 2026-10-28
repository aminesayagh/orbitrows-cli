"""Our AST (dml.py, ddl.py) → SQLGlot → SQLite SQL, and the check: SQLite itself, not our code (decided 2026-10-10).

    program(statements, schema)  the SQL of one prompt's statements, in order; the ids (t_variants, c_price_mad) are
                                 resolved against the store schema as earlier statements change it (a column added
                                 then filled, a table renamed then used)
    check(db, sql)               SQLite prepares each one (EXPLAIN: no row read, nothing run) and reports an unknown
                                 table or column, a syntax error…
    run(db, sql)                 runs them for real (a disposable copy of the store): the rows each select returns

Our code only resolves ids and maps one node to one SQLGlot class: a function is either one of the hand-mapped ones
(dml.MAPPED, SQLite's own forms) or one the operations catalog's inventory found SQLite runs (dml.FUNCTIONS: its
SQLGlot class, its fields in order). A select reading an earlier select (request_result)
becomes a CTE; a select inside a statement (a value, an IN list) becomes a subquery that may read the outer row. Not yet: a change reading an earlier request's rows (affected_rows: needs the executor).
"""
import re
import sqlite3

from pydantic import TypeAdapter
from sqlglot import exp

from . import dml
from .ddl import AlterTable, CreateTable, DropTable, NewTable, ddl

STATEMENT = TypeAdapter(dml.Statement)
DDL = {"create_table": CreateTable, "alter_table": AlterTable, "drop_table": DropTable}
COMPARE = {"=": exp.EQ, "!=": exp.NEQ, "<": exp.LT, "<=": exp.LTE, ">": exp.GT, ">=": exp.GTE}
ARITH = {"+": exp.Add, "-": exp.Sub, "*": exp.Mul, "/": exp.Div, "%": exp.Mod}
AGG = {"count": exp.Count, "sum": exp.Sum, "avg": exp.Avg, "min": exp.Min, "max": exp.Max,
       "group_concat": exp.GroupConcat}


def ident(name: str) -> exp.Identifier:
    return exp.to_identifier(name, quoted=True)


def parse(node: dict):
    """A statement dict → its model (Pydantic checks the shape)."""
    return DDL[node["type"]].model_validate(node) if node.get("type") in DDL else STATEMENT.validate_python(node)


class Registry:
    """ids → SQL names, for the store as the statements leave it."""

    def __init__(self, schema: list[dict]):
        self.tables = {t["definition"]["table_id"]: {"name": t["definition"]["sql_name"],
                                                    "columns": {c["column_id"]: c["sql_name"] for c in t["definition"]["columns"]}}
                       for t in schema}

    def table(self, tid: str) -> dict:
        if tid not in self.tables:
            raise ValueError(f"UNKNOWN_TABLE {tid}")
        return self.tables[tid]

    def column(self, tid: str, cid: str) -> str:
        cols = self.table(tid)["columns"]
        if cid not in cols:
            raise ValueError(f"UNKNOWN_COLUMN {cid} in {tid}")
        return cols[cid]


class Compiler:
    def __init__(self, registry: Registry, derived: dict, sources: dict, produced: dict):
        self.reg, self.derived, self.sources, self.produced = registry, derived, sources, produced
        self.expanding = []

    def value(self, n) -> exp.Expression:
        if isinstance(n, dml.Lit):
            v = n.value
            return (exp.Null() if v is None else exp.Boolean(this=v) if isinstance(v, bool)
                    else exp.Literal.number(v) if isinstance(v, (int, float)) else exp.Literal.string(v))
        if isinstance(n, dml.Column):
            src = self.sources.get(n.source)
            if src is None:
                raise ValueError(f"UNKNOWN_SOURCE {n.source}")
            name = n.reference if src[0] == "result" else self.reg.column(src[1], n.reference)
            return exp.column(ident(name), table=ident(n.source))
        if isinstance(n, dml.Arithmetic):
            return exp.Paren(this=ARITH[n.operator](this=self.value(n.left), expression=self.value(n.right)))
        if isinstance(n, dml.Negate):
            return exp.Neg(this=exp.Paren(this=self.value(n.operand)))
        if isinstance(n, dml.Case):
            return exp.Case(ifs=[exp.If(this=self.cond(b.when), true=self.value(b.then)) for b in n.branches],
                            default=self.value(n.else_))
        if isinstance(n, dml.FunctionCall):
            args = [self.value(a) for a in n.arguments]
            if n.name == "round":
                return exp.Round(this=args[0], decimals=args[1] if len(args) > 1 else None)
            if n.name == "coalesce":
                return exp.Coalesce(this=args[0], expressions=args[1:])
            if n.name == "replace":
                return exp.func("REPLACE", *args)
            if n.name == "real":
                return exp.cast(args[0], "REAL")
            if n.name == "nullif":
                return exp.Nullif(this=args[0], expression=args[1])
            if n.name in ("date", "julianday"):             # SQLite's date(value, modifier), julianday(value)
                return exp.Anonymous(this=n.name, expressions=args)
            if n.name == "substr":                          # SQLite's substr(text, start[, length])
                return exp.Anonymous(this="substr", expressions=args)
            if n.name in ("greatest", "least"):
                return (exp.Greatest if n.name == "greatest" else exp.Least)(this=args[0], expressions=args[1:])
            if n.name in dml.FUNCTIONS:                     # the catalog's: its SQLGlot class, its fields in order
                f = dml.FUNCTIONS[n.name]
                fields = dict(zip([x for x in f["fields"] if x != "expressions"], args))
                rest = args[len(fields):]
                return getattr(exp, f["class"])(**fields, **({"expressions": rest} if rest else {}))
            return {"trim": exp.Trim, "length": exp.Length, "lower": exp.Lower, "upper": exp.Upper,
                    "abs": exp.Abs}[n.name](this=args[0])
        if isinstance(n, dml.Concat):
            out = self.value(n.parts[0])
            for part in n.parts[1:]:
                out = exp.DPipe(this=out, expression=self.value(part))
            return exp.Paren(this=out)
        if isinstance(n, dml.Aggregate):
            arg = exp.Star() if n.argument is None else self.value(n.argument)
            return AGG[n.function](this=exp.Distinct(expressions=[arg]) if n.distinct else arg)
        if isinstance(n, dml.Subquery):
            return exp.Subquery(this=self.subquery(n.query))
        if isinstance(n, dml.DerivedReference):
            if n.reference in self.expanding:
                raise ValueError(f"CYCLIC_DEPENDENCY {' → '.join(self.expanding + [n.reference])}")
            if n.reference not in self.derived:
                raise ValueError(f"UNKNOWN_DERIVED {n.reference}")
            self.expanding.append(n.reference)
            out = self.value(TypeAdapter(dml.Expression).validate_python(self.derived[n.reference]))
            self.expanding.pop()
            return out
        raise ValueError(f"UNSUPPORTED_NODE {type(n).__name__}")

    def cond(self, n) -> exp.Expression:
        if isinstance(n, dml.Comparison):
            return COMPARE[n.operator](this=nocase(self.value(n.left), n.case), expression=self.value(n.right))
        if isinstance(n, (dml.All, dml.Any)):
            parts = [exp.Paren(this=self.cond(c)) for c in n.children]
            return exp.and_(*parts) if isinstance(n, dml.All) else exp.or_(*parts)
        if isinstance(n, dml.Not):
            return exp.Not(this=exp.Paren(this=self.cond(n.operand)))
        if isinstance(n, dml.NullCheck):
            out = exp.Is(this=self.value(n.expression), expression=exp.Null())
            return out if n.operator == "is_null" else exp.Not(this=out)
        if isinstance(n, dml.Membership):
            x = nocase(self.value(n.expression), n.case)
            out = (exp.In(this=x, query=exp.Subquery(this=self.subquery(n.query))) if n.query is not None else
                   exp.In(this=x, expressions=[self.value(v) for v in n.values]))
            return out if n.operator == "in" else exp.Not(this=out)
        if isinstance(n, dml.Between):
            out = exp.Between(this=self.value(n.expression), low=self.value(n.lower), high=self.value(n.upper))
            return exp.Not(this=out) if n.negated else out
        if isinstance(n, dml.Pattern):
            out = exp.Like(this=self.value(n.expression), expression=exp.Literal.string(n.pattern))
            if n.escape:
                out = exp.Escape(this=out, expression=exp.Literal.string(n.escape))
            return out if n.operator == "like" else exp.Not(this=exp.Paren(this=out))
        if isinstance(n, dml.EmptyCheck):                    # guide: NULL, '' or ordinary spaces only
            x = self.value(n.expression)
            out = exp.Paren(this=exp.or_(exp.Is(this=x, expression=exp.Null()),
                                         exp.EQ(this=exp.Trim(this=x.copy()), expression=exp.Literal.string(""))))
            return out if n.operator == "is_empty" else exp.Not(this=out)
        raise ValueError(f"UNSUPPORTED_NODE {type(n).__name__}")

    def subquery(self, q: dml.Select) -> exp.Select:
        """A select inside this statement: it may read the outer row's aliases (correlated)."""
        return _select(q, self.reg, self.derived, self.produced, outer=self.sources)

    def where(self, scope):
        return exp.Where(this=self.cond(scope.where)) if isinstance(scope, dml.Filtered) else None


def nocase(x: exp.Expression, case: str) -> exp.Expression:
    """A text compared ignoring letter case: x COLLATE NOCASE (SQLite folds ASCII letters only)."""
    return exp.Collate(this=x, expression=exp.Var(this="NOCASE")) if case == "ignore" else x


def _table(reg: Registry, src: dml.DatabaseTable) -> exp.Table:
    return exp.Table(this=ident(reg.table(src.reference)["name"]), alias=exp.TableAlias(this=ident(src.alias)))


def _ddl(s, reg: Registry) -> list[str]:
    """DDL statements, applying their effect to the registry (the planning copy of the store)."""
    if isinstance(s, CreateTable):
        reg.tables[s.definition.table_id] = {"name": s.definition.sql_name,
                                             "columns": {c.column_id: c.sql_name for c in s.definition.columns}}
        return [ddl(s.definition)]
    t = reg.table(s.target.reference)
    if isinstance(s, DropTable):
        del reg.tables[s.target.reference]
        return [exp.Drop(tables=[exp.Table(this=ident(t["name"]))], kind="TABLE").sql("sqlite")]
    out = []
    for a in s.actions:
        table = exp.Table(this=ident(t["name"]))
        if a.type == "add_column":
            d = a.definition
            if d.column_id in t["columns"] or d.sql_name in t["columns"].values():
                raise ValueError(f"COLUMN_ALREADY_EXISTS {d.sql_name}")
            constraints = ([] if d.nullable else [exp.ColumnConstraint(kind=exp.NotNullColumnConstraint())]) + (
                [exp.ColumnConstraint(kind=exp.DefaultColumnConstraint(this=Compiler(reg, {}, {}, {}).value(
                    dml.Lit(type="literal", value=d.default))))] if d.default is not None else [])
            action = exp.ColumnDef(this=ident(d.sql_name), kind=exp.DataType.build(d.sql_type, dialect="sqlite"),
                                   constraints=constraints)
            t["columns"][d.column_id] = d.sql_name
        elif a.type == "rename_column":
            action = exp.RenameColumn(this=ident(reg.column(s.target.reference, a.column)), to=ident(a.new_name))
            t["columns"][a.column] = a.new_name
        elif a.type == "rename_table":
            action = exp.AlterRename(this=exp.Table(this=ident(a.new_name)))
            t["name"] = a.new_name
        else:
            action = exp.Drop(tables=[exp.Column(this=ident(reg.column(s.target.reference, a.column)))], kind="COLUMN")
            del t["columns"][a.column]
        out.append(exp.Alter(this=table, kind="TABLE", actions=[action]).sql("sqlite"))
    return out


def _select(s: dml.Select, reg: Registry, derived: dict, produced: dict, outer: dict | None = None) -> exp.Select:
    src = s.from_
    if isinstance(src, dml.RequestResult):
        if src.request_id not in produced:
            raise ValueError(f"UNKNOWN_REQUEST {src.request_id} (a select reading an earlier select only)")
        sources = {src.alias: ("result", src.request_id)}
        frm = exp.Table(this=ident(src.request_id), alias=exp.TableAlias(this=ident(src.alias)))
    else:
        sources, frm = {src.alias: ("table", src.reference)}, _table(reg, src)
    if outer and src.alias in outer:
        raise ValueError(f"ALIAS_REUSED {src.alias} (a subquery's alias must differ from the outer one)")
    c = Compiler(reg, derived, {**(outer or {}), **sources}, produced)
    q = exp.select(*[exp.alias_(c.value(o.expression), o.alias, quoted=True) for o in s.columns]).from_(frm)
    if s.distinct:
        q = q.distinct()
    if (w := c.where(s.scope)) is not None:
        q = q.where(w.this)
    if s.group_by:
        q = q.group_by(*[c.value(g) for g in s.group_by])
    if s.having is not None:
        q = q.having(c.cond(s.having))
    if s.order_by:
        q = q.order_by(*[exp.Ordered(this=c.value(o.expression), desc=o.direction == "desc") for o in s.order_by])
    if s.limit is not None:
        q = q.limit(s.limit)
    if s.offset is not None:
        q = q.offset(s.offset)
    if isinstance(src, dml.RequestResult):
        q = q.with_(ident(src.request_id), as_=produced[src.request_id])
    return q


def program(statements: list[dict], schema: list[dict], derived: dict | None = None) -> list[str]:
    """SQL statements in execution order (one per DML statement, one per DDL action)."""
    reg, produced, out = Registry(schema), {}, []
    for node in statements:
        s = parse(node)
        if isinstance(s, (CreateTable, AlterTable, DropTable)):
            out += _ddl(s, reg)
            if isinstance(s, CreateTable) and s.query is not None:      # filled from data
                out.append(exp.insert(_select(s.query, reg, derived or {}, produced),
                                      exp.Table(this=ident(s.definition.sql_name)),
                                      columns=[ident(c.sql_name) for c in s.definition.columns]).sql("sqlite"))
            continue
        if isinstance(s, dml.Select):
            produced[s.id] = q = _select(s, reg, derived or {}, produced)
            out.append(q.sql("sqlite"))
            continue
        if isinstance(s, dml.Insert):
            cols = [ident(reg.column(s.target.reference, cid)) for cid in s.columns]
            table = exp.Table(this=ident(reg.table(s.target.reference)["name"]))
            if s.values is not None:
                c = Compiler(reg, derived or {}, {}, produced)    # values: no row to read from
                source = exp.values([tuple(c.value(v) for v in row) for row in s.values])
            else:
                source = _select(s.query, reg, derived or {}, produced)
            out.append(exp.insert(source, table, columns=cols).sql("sqlite"))
            continue
        c = Compiler(reg, derived or {}, {s.target.alias: ("table", s.target.reference)}, produced)
        table = _table(reg, s.target)
        if isinstance(s, dml.Update):
            sets = [exp.EQ(this=exp.column(ident(reg.column(s.target.reference, a.column.reference))),
                           expression=c.value(a.value)) for a in s.assignments]
            out.append(exp.Update(this=table, expressions=sets, where=c.where(s.scope)).sql("sqlite"))
        else:
            out.append(exp.Delete(this=table, where=c.where(s.scope)).sql("sqlite"))
    return out


def run(db: sqlite3.Connection, sql: list[str]) -> list:
    """Executes in order; per statement, the rows a select returns, else the number of rows changed."""
    out = []
    for q in sql:
        cur = db.execute(q)
        out.append(cur.fetchall() if cur.description else cur.rowcount)
    return out


def check(db: sqlite3.Connection, sql: list[str]) -> list[dict]:
    """SQLite's verdict on the statements as written (EXPLAIN prepares them: no row is read, nothing runs): [] when
    each is valid, else [{"statement", "sql", "error"}]. After a CREATE, ALTER or DROP the next ones depend on it:
    not checked. A value a column refuses (STRICT) is the execution's to report."""
    errors = []
    for i, q in enumerate(sql):
        try:
            db.execute(f"EXPLAIN {q}", {k: "[]" for k in re.findall(r":(rows_\w+)", q)}).fetchall()   # ids: none yet
        except sqlite3.Error as e:
            errors.append({"statement": i, "sql": q, "error": str(e)})
            break
        if re.match(r"\s*(CREATE|ALTER|DROP)\b", q, re.IGNORECASE):
            break
    return errors
