"""Store/incoming operations on the schema in models.py. Sync: call through asyncio.to_thread from the UI."""

from typing import Literal

from sqlalchemy import delete, func, insert, select
from sqlalchemy.dialects.postgresql import distinct_on

from orbitrows.services.db import connect
from orbitrows.services.db.models import (
    Incoming,
    IncomingColumn,
    IncomingEdit,
    IncomingRecord,
    IncomingRecordRevision,
    Store,
    StoreColumn,
    StoreEdit,
    StoreRecord,
    StoreRecordRevision,
)

Side = Literal["store", "incoming"]

# side -> (owner column name, column, record, revision, edit)
TABLES = {
    "store": ("store_id", StoreColumn, StoreRecord, StoreRecordRevision, StoreEdit),
    "incoming": ("incoming_id", IncomingColumn, IncomingRecord, IncomingRecordRevision, IncomingEdit),
}


def _write_rows(conn, side: Side, owner_id: int, header: list[str], rows: list[list[str]]) -> None:
    owner, Column, Record, Revision, _ = TABLES[side]
    conn.execute(insert(Column), [
        {owner: owner_id, "key": f"c{i}", "header": h, "position": i} for i, h in enumerate(header, 1)
    ])
    ids = conn.execute(
        insert(Record).returning(Record.id, sort_by_parameter_order=True), [{owner: owner_id}] * len(rows)
    ).scalars().all()
    conn.execute(insert(Revision), [
        # Raw strings, empty cells left out (doc: "Rules for cells").
        {"record_id": rid, "position": n, "cells": {f"c{i}": v for i, v in enumerate(row, 1) if v}}
        for n, (rid, row) in enumerate(zip(ids, rows), 1)
    ])


def upload_store(header: list[str], rows: list[list[str]]) -> int:
    with connect().begin() as conn:  # one transaction: store, columns, records, revisions
        store_id = conn.execute(insert(Store).returning(Store.id)).scalar_one()
        _write_rows(conn, "store", store_id, header, rows)
    return store_id


def upload_incoming(store_id: int, header: list[str], rows: list[list[str]]) -> int:
    with connect().begin() as conn:
        incoming_id = conn.execute(insert(Incoming).values(store_id=store_id).returning(Incoming.id)).scalar_one()
        _write_rows(conn, "incoming", incoming_id, header, rows)
    return incoming_id


def current(side: Side, owner_id: int, limit: int) -> tuple[list[str], list[list[str]], int]:
    """Latest version: (headers, first `limit` rows by position, total row count)."""
    owner, Column, Record, Revision, _ = TABLES[side]
    latest = (
        select(Revision)
        .join(Record, Record.id == Revision.record_id)
        .where(getattr(Record, owner) == owner_id)
        .ext(distinct_on(Revision.record_id))
        .order_by(Revision.record_id, Revision.edit_id.desc().nulls_last())  # latest edit first, upload last
        .subquery()
    )
    live = latest.c.cells.is_not(None)
    with connect().connect() as conn:
        columns = conn.execute(
            select(Column.key, Column.header).where(getattr(Column, owner) == owner_id).order_by(Column.position)
        ).all()
        cells = conn.execute(select(latest.c.cells).where(live).order_by(latest.c.position).limit(limit)).scalars()
        rows = [[c.get(key, "") for key, _ in columns] for c in cells]
        total = conn.execute(select(func.count()).where(live)).scalar_one()
    return [header for _, header in columns], rows, total


def undo(side: Side, owner_id: int) -> bool:
    """Remove the latest edit; cascades drop its revisions and records. False if there was none.

    Undoing an incoming edit that a merge used raises IntegrityError: reject the merge first.
    """
    owner, *_, Edit = TABLES[side]
    mine = getattr(Edit, owner) == owner_id
    with connect().begin() as conn:
        last = select(func.max(Edit.id)).where(mine).scalar_subquery()
        return conn.execute(delete(Edit).where(mine, Edit.id == last)).rowcount > 0


def reset(store_id: int) -> int:
    """Back to the uploaded store: remove every store edit (merges included). Returns how many."""
    with connect().begin() as conn:
        return conn.execute(delete(StoreEdit).where(StoreEdit.store_id == store_id)).rowcount
