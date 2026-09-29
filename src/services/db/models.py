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


# --- columns -----------------------------------------------------------------


class StoreColumn(Base):
    __tablename__ = "store_column"
    __table_args__ = (UniqueConstraint("store_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = ref("store.id", **CASCADE)
    key: Mapped[str] = mapped_column(String(16))
    header: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int]
    store_context_client: Mapped[int | None] = ref("store_column.id", ondelete="SET NULL")
    store_context_category: Mapped[int | None] = mapped_column(SmallInteger)  # 0 currency, 1 unit, 2 language
    header_context: Mapped[str | None] = mapped_column(Text)
    context_classify: Mapped[int | None] = mapped_column(SmallInteger)  # 0 dedicated, 1 header, 2 both, 3 none, 4 uncertain
    schema: Mapped[dict[str, Any] | None]


class IncomingColumn(Base):
    __tablename__ = "incoming_column"
    __table_args__ = (UniqueConstraint("incoming_id", "key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    incoming_id: Mapped[int] = ref("incoming.id", **CASCADE)
    key: Mapped[str] = mapped_column(String(16))
    header: Mapped[str] = mapped_column(Text, default="")
    position: Mapped[int]
    incoming_context_client: Mapped[int | None] = ref("incoming_column.id", ondelete="SET NULL")
    incoming_context_category: Mapped[int | None] = mapped_column(SmallInteger)
    header_context: Mapped[str | None] = mapped_column(Text)
    context_classify: Mapped[int | None] = mapped_column(SmallInteger)


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


# --- edits -------------------------------------------------------------------


class IncomingEdit(Created, Base):
    __tablename__ = "incoming_edit"
    id: Mapped[int] = mapped_column(primary_key=True)
    incoming_id: Mapped[int] = ref("incoming.id", **CASCADE)
    prompt: Mapped[str] = mapped_column(Text)
    update: Mapped[dict[str, Any]]
    confidence: Mapped[float | None]  # ponytail: meaning still undefined (doc §7)


class StoreEdit(Created, Base):
    __tablename__ = "store_edit"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = ref("store.id", **CASCADE)
    prompt: Mapped[str] = mapped_column(Text)
    update: Mapped[dict[str, Any]]
    confidence: Mapped[float | None]
    # Set = this edit is a merge. NO ACTION: reject the merge before deleting/undoing what it merged.
    incoming_id: Mapped[int | None] = ref("incoming.id")
    incoming_edit_id: Mapped[int | None] = ref("incoming_edit.id")


# --- records + revisions -----------------------------------------------------


class StoreRecord(Base):
    __tablename__ = "store_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = ref("store.id", **CASCADE)
    edit_id: Mapped[int | None] = ref("store_edit.id", **CASCADE)  # null = uploaded


class IncomingRecord(Base):
    __tablename__ = "incoming_record"
    id: Mapped[int] = mapped_column(primary_key=True)
    incoming_id: Mapped[int] = ref("incoming.id", **CASCADE)
    edit_id: Mapped[int | None] = ref("incoming_edit.id", **CASCADE)


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
    edit_id: Mapped[int | None] = ref("store_edit.id", **CASCADE)  # null = uploaded content
    position: Mapped[Decimal] = mapped_column(Numeric)  # exact, so midpoints never collide
    cells: Mapped[dict[str, Any] | None]  # raw CSV strings keyed by column key; null = deleted


class IncomingRecordRevision(Base):
    __tablename__ = "incoming_record_revision"
    __table_args__ = _revision_args(__tablename__)
    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = ref("incoming_record.id", **CASCADE)
    edit_id: Mapped[int | None] = ref("incoming_edit.id", **CASCADE)
    position: Mapped[Decimal] = mapped_column(Numeric)
    cells: Mapped[dict[str, Any] | None]


# --- merge -------------------------------------------------------------------


class MergeRow(Base):
    __tablename__ = "merge_row"
    __table_args__ = (
        UniqueConstraint("edit_id", "incoming_record_id", "record_id", postgresql_nulls_not_distinct=True),
    )
    id: Mapped[int] = mapped_column(primary_key=True)
    edit_id: Mapped[int] = ref("store_edit.id", **CASCADE)
    incoming_record_id: Mapped[int] = ref("incoming_record.id")
    record_id: Mapped[int | None] = ref("store_record.id", **CASCADE)  # null = no product matched
    issues: Mapped[dict[str, Any] | None]  # {"c9": {"status": ..., "reason": ...}}; null = clean
