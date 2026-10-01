"""Schema invariants from doc/database-structure.md. Needs a migrated DATABASE_URL; rolls back everything."""

import os

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orbitrows.services.db import connect
from orbitrows.services.db.models import (
    Edit,
    Incoming,
    IncomingRecord,
    IncomingRecordRevision,
    Store,
    StoreColumn,
    StoreColumnRevision as ColRev,
    StoreRecord,
    StoreRecordRevision as Rev,
)

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="DATABASE_URL not set")


def latest(table: str, identity: str, parent: str, keep: str, show: str) -> text:
    """Doc §4 "Reading": latest revision per identity, then drop deletes, ordered by position."""
    return text(f"""
        SELECT {identity}, {show} FROM (
          SELECT DISTINCT ON ({identity}) * FROM {table}_revision
          WHERE {identity} IN (SELECT id FROM {table} WHERE {parent} = :p)
          ORDER BY {identity}, edit_id DESC NULLS LAST
        ) cur WHERE {keep} ORDER BY position
    """)


RECORDS = latest("store_record", "record_id", "store_id", "cells IS NOT NULL", "cells")
COLUMNS = latest("store_column", "column_id", "store_id", "NOT deleted", "header")
INCOMING = latest("incoming_record", "record_id", "incoming_id", "cells IS NOT NULL", "cells")


def test_one_edit_on_both_sides_then_undo():
    with connect().connect() as conn, conn.begin() as tx, Session(conn) as s:
        store = Store()
        s.add(store)
        s.flush()
        incoming = Incoming(store_id=store.id)
        col = StoreColumn(store_id=store.id, key="c1")
        a, b = StoreRecord(store_id=store.id), StoreRecord(store_id=store.id)
        s.add_all([incoming, col, a, b])
        s.flush()
        x = IncomingRecord(incoming_id=incoming.id)
        s.add(x)
        s.flush()
        # Upload: identity + first revision (edit_id null) for everything.
        s.add_all([
            ColRev(column_id=col.id, header="Poids (kg)", position=1, header_context="kg"),
            Rev(record_id=a.id, position=1, cells={"c1": "007"}),
            Rev(record_id=b.id, position=2, cells={"c1": "B"}),
            IncomingRecordRevision(record_id=x.id, position=1, cells={"c1": "X"}),
        ])
        s.flush()

        # One prompt edits both sides: change a, delete b, add c, rename the column, change the incoming row.
        edit = Edit(store_id=store.id, source=0, prompt="p", update={"$set": {}})
        s.add(edit)
        c = StoreRecord(store_id=store.id)  # identity only: "created by" is its first revision's edit
        s.add(c)
        s.flush()
        s.add_all([
            Rev(record_id=a.id, edit_id=edit.id, position=1, cells={"c1": "008"}),
            Rev(record_id=b.id, edit_id=edit.id, position=2, cells=None),
            Rev(record_id=c.id, edit_id=edit.id, position=1.5, cells={"c1": "C"}),
            ColRev(column_id=col.id, edit_id=edit.id, header="Poids (g)", position=1, header_context="g"),
            IncomingRecordRevision(record_id=x.id, edit_id=edit.id, position=1, cells={"c1": "Y"}),
        ])
        s.flush()
        sid, iid, ai, bi, ci, coli = store.id, incoming.id, a.id, b.id, c.id, col.id  # ints survive expire_all
        read = lambda q, p: [tuple(r) for r in s.execute(q, {"p": p})]
        assert read(RECORDS, sid) == [(ai, {"c1": "008"}), (ci, {"c1": "C"})]
        assert read(COLUMNS, sid) == [(coli, "Poids (g)")]
        assert read(INCOMING, iid)[0][1] == {"c1": "Y"}

        # Undo = one DELETE on the single timeline; cascades remove its revisions on both sides.
        s.execute(delete(Edit).where(Edit.store_id == sid, Edit.id >= edit.id))
        s.expire_all()
        assert read(RECORDS, sid) == [(ai, {"c1": "007"}), (bi, {"c1": "B"})]
        assert read(COLUMNS, sid) == [(coli, "Poids (kg)")]
        assert read(INCOMING, iid)[0][1] == {"c1": "X"}
        assert s.get(StoreRecord, ci) is not None  # identity left with no revision: exists in no version

        # NULLS NOT DISTINCT: a second upload revision for the same record or column is refused.
        for duplicate in (Rev(record_id=ai, position=9, cells={}), ColRev(column_id=coli, header="x", position=9)):
            with pytest.raises(IntegrityError), s.begin_nested():
                s.add(duplicate)
                s.flush()
        tx.rollback()
