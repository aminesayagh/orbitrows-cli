"""Schema invariants from doc/database-structure.md. Needs a migrated DATABASE_URL; rolls back everything."""

import os

import pytest
from sqlalchemy import delete, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from orbitrows.services.db import connect
from orbitrows.services.db.models import Store, StoreEdit, StoreRecord, StoreRecordRevision as Rev

pytestmark = pytest.mark.skipif("DATABASE_URL" not in os.environ, reason="DATABASE_URL not set")

CURRENT = text("""
    SELECT record_id, cells FROM (
      SELECT DISTINCT ON (record_id) * FROM store_record_revision
      WHERE record_id IN (SELECT id FROM store_record WHERE store_id = :s)
      ORDER BY record_id, edit_id DESC NULLS LAST
    ) cur WHERE cells IS NOT NULL ORDER BY position
""")


def test_edit_then_undo():
    with connect().connect() as conn, conn.begin() as tx, Session(conn) as s:
        store = Store()
        s.add(store)
        s.flush()
        a, b = StoreRecord(store_id=store.id), StoreRecord(store_id=store.id)
        s.add_all([a, b])
        s.flush()
        s.add_all([Rev(record_id=a.id, position=1, cells={"c1": "007"}), Rev(record_id=b.id, position=2, cells={"c1": "B"})])
        s.flush()

        # Edit: change a, delete b, add c.
        edit = StoreEdit(store_id=store.id, prompt="p", update={"$set": {}})
        s.add(edit)
        s.flush()
        c = StoreRecord(store_id=store.id, edit_id=edit.id)
        s.add(c)
        s.flush()
        s.add_all([
            Rev(record_id=a.id, edit_id=edit.id, position=1, cells={"c1": "008"}),
            Rev(record_id=b.id, edit_id=edit.id, position=2, cells=None),
            Rev(record_id=c.id, edit_id=edit.id, position=1.5, cells={"c1": "C"}),
        ])
        s.flush()
        sid, ai, bi, ci = store.id, a.id, b.id, c.id  # plain ints survive expire_all below
        current = lambda: [tuple(r) for r in s.execute(CURRENT, {"s": sid})]
        assert current() == [(ai, {"c1": "008"}), (ci, {"c1": "C"})]

        # Undo = one DELETE; cascades remove the edit's revisions and the record it added.
        s.execute(delete(StoreEdit).where(StoreEdit.store_id == sid, StoreEdit.id >= edit.id))
        s.expire_all()
        assert current() == [(ai, {"c1": "007"}), (bi, {"c1": "B"})]
        assert s.get(StoreRecord, ci) is None

        # NULLS NOT DISTINCT: a second upload revision for the same record is refused.
        with pytest.raises(IntegrityError), s.begin_nested():
            s.add(Rev(record_id=ai, position=9, cells={}))
            s.flush()
        tx.rollback()
