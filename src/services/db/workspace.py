"""Store/incoming operations on the schema in models.py. Sync: call through asyncio.to_thread from the UI.

Every identity (column, record) is written with its first revision (edit_id = null for the upload), and the
state at any version is the latest revision per identity (doc/database-structure.md §4 "Reading").
"""

from typing import Literal

from sqlalchemy import delete, exists, func, insert, or_, select
from sqlalchemy.dialects.postgresql import distinct_on

from orbitrows.pipeline.context import CATEGORIES, CLASSIFY, ColumnProfile
from orbitrows.services.db import connect
from orbitrows.services.db.models import (
    Edit,
    Incoming,
    IncomingColumn,
    IncomingColumnRevision,
    IncomingRecord,
    IncomingRecordRevision,
    Store,
    StoreColumn,
    StoreColumnRevision,
    StoreRecord,
    StoreRecordRevision,
)

Side = Literal["store", "incoming"]

# side -> (owner column name, column, column revision, record, record revision)
TABLES = {
    "store": ("store_id", StoreColumn, StoreColumnRevision, StoreRecord, StoreRecordRevision),
    "incoming": ("incoming_id", IncomingColumn, IncomingColumnRevision, IncomingRecord, IncomingRecordRevision),
}


def _write_upload(
    conn, side: Side, owner_id: int, header: list[str], rows: list[list[str]],
    column_state: list[dict] | None = None, clients: dict[str, str] | None = None,
) -> None:
    """Identities + first revisions (edit_id null) for every column and row.

    `column_state`: extra revision fields per column, in order. `clients`: column key -> key of its context column.
    """
    owner, Column, ColumnRevision, Record, Revision = TABLES[side]
    column_ids = conn.execute(
        insert(Column).returning(Column.id, sort_by_parameter_order=True),
        [{owner: owner_id, "key": f"c{i}"} for i in range(1, len(header) + 1)],
    ).scalars().all()
    id_of = {f"c{i}": cid for i, cid in enumerate(column_ids, 1)}
    conn.execute(insert(ColumnRevision), [
        {"column_id": cid, "header": h, "position": i, "deleted": False, "context_client": None}
        | (column_state[i - 1] if column_state else {})
        | ({"context_client": id_of[clients[f"c{i}"]]} if clients and f"c{i}" in clients else {})
        for i, (cid, h) in enumerate(zip(column_ids, header), 1)
    ])
    record_ids = conn.execute(
        insert(Record).returning(Record.id, sort_by_parameter_order=True), [{owner: owner_id}] * len(rows)
    ).scalars().all()
    conn.execute(insert(Revision), [
        # Raw strings, empty cells left out (doc: "Rules for cells").
        {"record_id": rid, "position": n, "cells": {f"c{i}": v for i, v in enumerate(row, 1) if v}}
        for n, (rid, row) in enumerate(zip(record_ids, rows), 1)
    ])


def upload_store(header: list[str], rows: list[list[str]], context: dict[str, ColumnProfile]) -> int:
    """One transaction: store, columns (schema + context), records. `context` is keyed c1, c2…"""
    profiles = [context[f"c{i}"] for i in range(1, len(header) + 1)]
    column_state = [
        {
            # A Table Schema field; name is the original header, even empty or duplicated (key/position identify it).
            "schema": {"name": h, "type": c.type},
            "context_classify": CLASSIFY[c.role],
            "context_category": CATEGORIES.index(c.category) if c.category else None,
            "header_context": c.header_context,
        }
        for h, c in zip(header, profiles)
    ]
    clients = {k: c.client for k, c in context.items() if c.client}
    with connect().begin() as conn:
        store_id = conn.execute(insert(Store).returning(Store.id)).scalar_one()
        _write_upload(conn, "store", store_id, header, rows, column_state, clients)
    return store_id


def upload_incoming(store_id: int, header: list[str], rows: list[list[str]]) -> int:
    with connect().begin() as conn:
        incoming_id = conn.execute(insert(Incoming).values(store_id=store_id).returning(Incoming.id)).scalar_one()
        _write_upload(conn, "incoming", incoming_id, header, rows)
    return incoming_id


def _latest(Revision, identity, Identity, owner_filter):
    """Latest revision per identity (DISTINCT ON), as a subquery."""
    return (
        select(Revision)
        .join(Identity, Identity.id == identity)
        .where(owner_filter)
        .ext(distinct_on(identity))
        .order_by(identity, Revision.edit_id.desc().nulls_last())  # latest edit first, upload last
        .subquery()
    )


def current(side: Side, owner_id: int, limit: int) -> tuple[list[str], list[list[str]], int]:
    """Latest version: (headers, first `limit` rows by position, total row count)."""
    owner, Column, ColumnRevision, Record, Revision = TABLES[side]
    columns = _latest(ColumnRevision, ColumnRevision.column_id, Column, getattr(Column, owner) == owner_id)
    records = _latest(Revision, Revision.record_id, Record, getattr(Record, owner) == owner_id)
    live = records.c.cells.is_not(None)
    with connect().connect() as conn:
        shown = conn.execute(
            select(Column.key, columns.c.header)
            .join(columns, columns.c.column_id == Column.id)
            .where(~columns.c.deleted)
            .order_by(columns.c.position)
        ).all()
        cells = conn.execute(select(records.c.cells).where(live).order_by(records.c.position).limit(limit)).scalars()
        rows = [[c.get(key, "") for key, _ in shown] for c in cells]
        total = conn.execute(select(func.count()).where(live)).scalar_one()
    return [header for _, header in shown], rows, total


def _touched(side: Side, edit_id) -> exists:
    """The edit wrote at least one revision on this side."""
    _, _, ColumnRevision, _, Revision = TABLES[side]
    return or_(
        exists().where(Revision.edit_id == edit_id),
        exists().where(ColumnRevision.edit_id == edit_id),
    )


def undo(store_id: int, side: Side) -> Literal["undone", "nothing", "other_side"]:
    """Remove the session's latest edit (one timeline, doc §6), if it changed `side`.

    Cascades drop every revision it wrote, on both sides. An edit on the other file is left alone.
    """
    with connect().begin() as conn:
        last = conn.execute(select(func.max(Edit.id)).where(Edit.store_id == store_id)).scalar_one()
        if last is None:
            return "nothing"
        if not conn.execute(select(_touched(side, last))).scalar_one():
            return "other_side"
        conn.execute(delete(Edit).where(Edit.id == last))
        return "undone"


def reset(store_id: int) -> int:
    """Back to the uploaded files: remove every edit of the session (merges included). Returns how many."""
    with connect().begin() as conn:
        return conn.execute(delete(Edit).where(Edit.store_id == store_id)).rowcount
