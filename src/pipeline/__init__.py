"""Import pipeline (see docs/data-structures.md in the PoC).

store/  the store import: from the uploaded CSV files to the store schema, its metadata and the round-trip proof
        (steps in store/__init__.py, chained by store/run.py)

Next, for an incoming file: link its columns to the store's, match its rows to the store's records, convert its
values and stage the proposed updates.
"""
