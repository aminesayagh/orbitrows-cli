"""The create_table AST node (vault poc_v2/sql-ast-experimentation-guide.md §12, DDL contract), as Pydantic models.
No SQL is produced: the node is the output. SQLite itself checks it, by creating it as an in-memory STRICT table.

    sql_type   one of SQLite's STRICT types; a type follows the cells' original format, not their meaning
               ("21,90 €" is TEXT)
    header     the merchant's original column name, kept for the export (not in the guide's contract); None for a
               column the import adds (the `id` key of a table without one)
"""
import re
import sqlite3
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, model_validator

from ..units.dictionary import plain
from .dml import Select

# ponytail: SQLite has no API listing its types; these are the STRICT-table types (ANY left out: it means no type)
SQLITE_TYPES = ("INTEGER", "REAL", "TEXT", "BLOB")


class NewColumn(BaseModel):
    column_id: str
    sql_name: str
    header: str | None
    sql_type: Literal[SQLITE_TYPES]
    nullable: bool
    default: str | int | float | bool | None = None           # guide §12: a literal default (an expression: not yet)


class TableColumns(BaseModel):
    table: str
    columns: list[str] = Field(min_length=1)


ACTIONS = ("no_action", "restrict", "set_null", "set_default", "cascade")      # SQLite's ON DELETE / ON UPDATE


class ForeignKey(BaseModel):
    columns: list[str] = Field(min_length=1)          # local column ids
    references: TableColumns                          # the other table's primary key
    # defaults (user's example, 2026-10-09): a referenced record cannot be deleted while rows use it; a changed key
    # follows into the rows that use it
    on_delete: Literal[ACTIONS] = "restrict"
    on_update: Literal[ACTIONS] = "cascade"


class NewTable(BaseModel):
    table_id: str
    sql_name: str
    columns: list[NewColumn] = Field(min_length=1)
    primary_key: list[str] = []
    foreign_keys: list[ForeignKey] = []

    @model_validator(mode="after")
    def sqlite_accepts(self):
        """Key columns exist; SQLite rejects an unknown type, a repeated column name or a bad primary key
        (identifiers come from sql_name(): [a-z0-9_])."""
        names = {c.column_id for c in self.columns}
        for k in self.primary_key + [c for fk in self.foreign_keys for c in fk.columns]:
            if k not in names:
                raise ValueError(f"UNKNOWN_COLUMN {k} in {self.table_id}")
        with sqlite3.connect(":memory:") as db:
            db.execute(ddl(self))
        return self


def ddl(t: NewTable, tables: dict | None = None) -> str:
    """The CREATE TABLE text SQLite checks (validation and the step-7 round trip only; the AST is the output).
    tables: {table_id: NewTable} of the whole store, to write the FOREIGN KEY clauses (one table alone cannot name
    the tables it references)."""
    names = {c.column_id: c.sql_name for c in t.columns}
    cols = [f'"{c.sql_name}" {c.sql_type}{"" if c.nullable else " NOT NULL"}' for c in t.columns]
    if t.primary_key:
        cols.append("PRIMARY KEY (" + ", ".join(f'"{names[k]}"' for k in t.primary_key) + ")")
    for fk in t.foreign_keys if tables else []:
        target = tables[fk.references.table]
        tn = {c.column_id: c.sql_name for c in target.columns}
        cols.append(f'FOREIGN KEY ({", ".join(chr(34) + names[k] + chr(34) for k in fk.columns)}) '
                    f'REFERENCES "{target.sql_name}" ({", ".join(chr(34) + tn[k] + chr(34) for k in fk.references.columns)}) '
                    f'ON DELETE {fk.on_delete.replace("_", " ").upper()} ON UPDATE {fk.on_update.replace("_", " ").upper()}')
    return f'CREATE TABLE "{t.sql_name}" ({", ".join(cols)}) STRICT'


class CreateTable(BaseModel):
    """CREATE TABLE, empty or filled from a select ("a backup copy of suppliers"): written as CREATE TABLE then
    INSERT … SELECT, so the declared types, keys and STRICT hold (SQLite's CREATE TABLE … AS SELECT drops them)."""
    id: str
    type: Literal["create_table"] = "create_table"
    definition: NewTable
    query: Select | None = Field(None, exclude_if=lambda q: q is None)   # CLI: the import's dumps stay as they were

    @model_validator(mode="after")
    def query_fits(self):
        if self.query is not None and len(self.query.columns) != len(self.definition.columns):
            raise ValueError("the query returns one value per column of the new table")
        return self


# Schema edits (guide §12): one SQL statement per action, applied in order
class TableRef(BaseModel):
    type: Literal["database_table"]
    reference: str


class AddColumn(BaseModel):
    type: Literal["add_column"]
    definition: NewColumn


class RenameColumn(BaseModel):
    type: Literal["rename_column"]
    column: str
    new_name: str


class RenameTable(BaseModel):
    type: Literal["rename_table"]
    new_name: str


class DropColumn(BaseModel):
    type: Literal["drop_column"]
    column: str


class AlterTable(BaseModel):
    id: str
    type: Literal["alter_table"]
    target: TableRef
    actions: list[Annotated[Union[AddColumn, RenameColumn, RenameTable, DropColumn], Field(discriminator="type")]] = \
        Field(min_length=1)


class DropTable(BaseModel):
    id: str
    type: Literal["drop_table"]
    target: TableRef


def check_references(tables: list[NewTable]) -> list[tuple]:
    """[(table_id, problem)] across the tables of one case: a foreign key must point at another table's primary key,
    with the same SQLite types. [] when the references hold."""
    by_id, problems = {t.table_id: t for t in tables}, []
    for t in tables:
        types = {c.column_id: c.sql_type for c in t.columns}
        for fk in t.foreign_keys:
            target = by_id.get(fk.references.table)
            if not target:
                problems.append((t.table_id, f"UNKNOWN_TABLE {fk.references.table}"))
            elif fk.references.columns != target.primary_key or len(fk.columns) != len(fk.references.columns):
                problems.append((t.table_id, f"{fk.columns} does not reference the primary key of {target.table_id}"))
            else:
                target_types = {c.column_id: c.sql_type for c in target.columns}
                for a, b in zip(fk.columns, fk.references.columns):
                    if types[a] != target_types[b]:
                        problems.append((t.table_id, f"TYPE_MISMATCH {a} {types[a]} → {target.table_id}.{b} {target_types[b]}"))
    return problems


def _strict_type(declared: str) -> str:
    """SQLite's affinity rules (sqlite.org/datatype3.html §3.1) → a STRICT type."""
    d = declared.upper()
    if "INT" in d:
        return "INTEGER"
    if any(w in d for w in ("CHAR", "CLOB", "TEXT")):
        return "TEXT"
    if "BLOB" in d or not d:
        return "BLOB"
    # ponytail: REAL, FLOAT, DOUBLE, and NUMERIC/DECIMAL (no STRICT equivalent) → REAL; finer AST types: notice #4
    return "REAL"


def schema_from_sqlite(db: sqlite3.Connection) -> list[CreateTable]:
    """The create_table nodes of an existing SQLite database (production stores arrive as SQLite): ids t_<table> and
    c_<column>, types by affinity, NOT NULL, primary keys and foreign keys with their actions, read from the database."""
    nodes = []
    for (table,) in db.execute("SELECT name FROM sqlite_schema WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY rowid"):
        info = db.execute(f'PRAGMA table_info("{table}")').fetchall()          # cid, name, type, notnull, default, pk
        fks = {}
        for _, seq, target, col, to, on_update, on_delete, _ in db.execute(f'PRAGMA foreign_key_list("{table}")'):
            fk = fks.setdefault((target, on_update, on_delete), ([], []))
            fk[0].append(f"c_{col}")
            fk[1].append(f"c_{to}")
        nodes.append(CreateTable(id=f"r_create_{table}", definition=NewTable(
            table_id=f"t_{table}", sql_name=table,
            columns=[NewColumn(column_id=f"c_{n}", sql_name=n, header=n, sql_type=_strict_type(t), nullable=not (nn or pk))
                     for _, n, t, nn, _, pk in info],
            primary_key=[f"c_{r[1]}" for r in sorted(info, key=lambda r: r[5]) if r[5]],
            foreign_keys=[ForeignKey(columns=a, references=TableColumns(table=f"t_{target}", columns=b),
                                     on_update=up.lower().replace(" ", "_"), on_delete=de.lower().replace(" ", "_"))
                          for (target, up, de), (a, b) in fks.items()])))
    return nodes


def sql_name(text: str, taken=(), default="col") -> str:
    """A SQL identifier from a header or file name: "Prix TTC" → prix_ttc; a repeated name gets _2, _3…"""
    base = re.sub(r"[^a-z0-9]+", "_", plain(text)).strip("_") or default
    name, n = base, 1
    while name in taken:
        n += 1
        name = f"{base}_{n}"
    return name


def create_table(table: str, columns: list[dict]) -> CreateTable:
    """columns: [{"header", "sql_type", "nullable"}] in file order → the validated create_table node."""
    names = []
    for i, c in enumerate(columns, 1):
        names.append(sql_name(c["header"], names, f"col_{i}"))
    t = sql_name(table, default="imported")
    return CreateTable(id=f"r_create_{t}", definition=NewTable(
        table_id=f"t_{t}", sql_name=t,
        columns=[NewColumn(column_id=f"c_{n}", sql_name=n, **c) for n, c in zip(names, columns)]))

