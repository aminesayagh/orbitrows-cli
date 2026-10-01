"""PostgreSQL schema. See doc/database-structure.md for the why of each table."""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    REAL,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Mongo-style JSON; None -> SQL NULL (a deleted revision), not JSON 'null'.
Json = JSONB(none_as_null=True)

CASCADE = {"ondelete": "CASCADE"}


class Base(DeclarativeBase):
    type_annotation_map = {int: BigInteger, float: REAL, dict[str, Any]: Json}


class Created:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def ref(target: str, **kw) -> Any:
    """Foreign key column. No ondelete = NO ACTION: Postgres refuses the delete while the row is still referenced."""
    return mapped_column(ForeignKey(target, **kw), index=True)


# --- roots -------------------------------------------------------------------


class Store(Created, Base):
    __tablename__ = "store"
    id: Mapped[int] = mapped_column(primary_key=True)


class Incoming(Created, Base):
    __tablename__ = "incoming"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = ref("store.id", **CASCADE)


# --- edits -------------------------------------------------------------------


class Edit(Created, Base):
    """One timeline per store: store AND incoming revisions point here, so one prompt can edit both as one edit."""
    __tablename__ = "edit"
    id: Mapped[int] = mapped_column(primary_key=True)  # increasing = edit order
    store_id: Mapped[int] = ref("store.id", **CASCADE)
    source: Mapped[int] = mapped_column(SmallInteger)  # 0 prompt, 1 merge, 2 profile, 3 manual
    prompt: Mapped[str | None] = mapped_column(Text)
    update: Mapped[dict[str, Any] | None]  # the executed Mongo update JSON
    # Set = merge. NO ACTION: reject the merge before deleting the incoming it merged.
    incoming_id: Mapped[int | None] = ref("incoming.id")


# --- columns: identity + revision --------------------------------------------
# Identities hold only what never changes; everything else is a revision (doc §2).


class StoreColumn(Base):
    __tablename__ = "store_column"
    __table_args__ = (UniqueConstraint("store_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = ref("store.id", **CASCADE)
    key: Mapped[str] = mapped_column(String(16))  # "c1": the key in cells, never changes


class IncomingColumn(Base):
    __tablename__ = "incoming_column"
    __table_args__ = (UniqueConstraint("incoming_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    incoming_id: Mapped[int] = ref("incoming.id", **CASCADE)
    key: Mapped[str] = mapped_column(String(16))


def _column_revision_args() -> tuple:
    return (UniqueConstraint("column_id", "edit_id", postgresql_nulls_not_distinct=True),)


class StoreColumnRevision(Base):
    __tablename__ = "store_column_revision"
    __table_args__ = _column_revision_args()
    id: Mapped[int] = mapped_column(primary_key=True)  # ORM PK; (column_id, edit_id) has a nullable part
    column_id: Mapped[int] = ref("store_column.id", **CASCADE)
    edit_id: Mapped[int | None] = ref("edit.id", **CASCADE)  # null = as uploaded
    header: Mapped[str] = mapped_column(Text, default="")  # "" = no header
    position: Mapped[Decimal] = mapped_column(Numeric)
    deleted: Mapped[bool] = mapped_column(default=False)  # a flag, since an empty header is legitimate
    context_classify: Mapped[int | None] = mapped_column(SmallInteger)  # 0 dedicated, 1 header, 2 both, 3 none, 4 uncertain
    # 0 currency, 1 physical_unit, 2 counting_unit, 3 compound_unit, 4 percentage, 5 language, 6 other, 7 unknown (doc §7)
    context_category: Mapped[int | None] = mapped_column(SmallInteger)
    context_client: Mapped[int | None] = ref("store_column.id", ondelete="SET NULL")  # the IDENTITY carrying the context
    header_context: Mapped[str | None] = mapped_column(Text)  # unit read from the header ("kg")
    schema: Mapped[dict[str, Any] | None]  # Table Schema field; store only


class IncomingColumnRevision(Base):
    __tablename__ = "incoming_column_revision"
    __table_args__ = _column_revision_args()
    id: Mapped[int] = mapped_column(primary_key=True)
    column_id: Mapped[int] = ref("incoming_column.id", **CASCADE)
    edit_id: Mapped[int | None] = ref("edit.id", **CASCADE)
    header: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[Decimal] = mapped_column(Numeric)
    deleted: Mapped[bool] = mapped_column(default=False)
    context_classify: Mapped[int | None] = mapped_column(SmallInteger)
    context_category: Mapped[int | None] = mapped_column(SmallInteger)
    context_client: Mapped[int | None] = ref("incoming_column.id", ondelete="SET NULL")
    header_context: Mapped[str | None] = mapped_column(Text)
    # no schema: profiling is store-only


class ColumnMatch(Base):
    __tablename__ = "column_match"
    incoming_column_id: Mapped[int] = mapped_column(ForeignKey("incoming_column.id", **CASCADE), primary_key=True)
    store_column_id: Mapped[int] = mapped_column(ForeignKey("store_column.id", **CASCADE), primary_key=True, index=True)
    link: Mapped[int] = mapped_column(SmallInteger)  # 0 direct, 1 partial
    confidence: Mapped[float]
    role: Mapped[int] = mapped_column(SmallInteger)  # 0 dedicated_key, 1 descriptive_base, 2 qualifier, 3 none
    role_confidence: Mapped[float]
    level: Mapped[int | None] = mapped_column(SmallInteger)  # 0 individual_record, 1 product_family
    level_confidence: Mapped[float | None]


# --- records: identity + revision --------------------------------------------


class StoreRecord(Base):
    __tablename__ = "store_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = ref("store.id", **CASCADE)
    # no edit_id: whether it exists at version k is answered by its revisions


class IncomingRecord(Base):
    __tablename__ = "incoming_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    incoming_id: Mapped[int] = ref("incoming.id", **CASCADE)


def _revision_args(table: str) -> tuple:
    return (
        # One revision per record per edit; NULLS NOT DISTINCT makes the upload revision unique too (PG15+).
        UniqueConstraint("record_id", "edit_id", postgresql_nulls_not_distinct=True),
        Index(f"ix_{table}_cells", "cells", postgresql_using="gin", postgresql_ops={"cells": "jsonb_path_ops"}),
    )


class StoreRecordRevision(Base):
    __tablename__ = "store_record_revision"
    __table_args__ = _revision_args(__tablename__)
    id: Mapped[int] = mapped_column(primary_key=True)  # ORM needs a PK; (record_id, edit_id) has a nullable part
    record_id: Mapped[int] = ref("store_record.id", **CASCADE)
    edit_id: Mapped[int | None] = ref("edit.id", **CASCADE)  # null = uploaded content
    position: Mapped[Decimal] = mapped_column(Numeric)  # exact, so midpoints never collide
    cells: Mapped[dict[str, Any] | None]  # raw CSV strings keyed by column key; null = deleted


class IncomingRecordRevision(Base):
    __tablename__ = "incoming_record_revision"
    __table_args__ = _revision_args(__tablename__)
    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = ref("incoming_record.id", **CASCADE)
    edit_id: Mapped[int | None] = ref("edit.id", **CASCADE)
    position: Mapped[Decimal] = mapped_column(Numeric)
    cells: Mapped[dict[str, Any] | None]


# --- merge -------------------------------------------------------------------


class MergeRow(Base):
    __tablename__ = "merge_row"
    __table_args__ = (
        UniqueConstraint("edit_id", "incoming_record_id", "record_id", postgresql_nulls_not_distinct=True),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    edit_id: Mapped[int] = ref("edit.id", **CASCADE)
    incoming_record_id: Mapped[int] = ref("incoming_record.id")
    record_id: Mapped[int | None] = ref("store_record.id", **CASCADE)  # null = no product matched
    issues: Mapped[dict[str, Any] | None]  # {"c9": {"status": ..., "reason": ...}}; null = clean
