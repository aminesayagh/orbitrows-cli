"""Operations: the catalog of what a message can ask SQL to compute (functions, operators, window functions, and the
compositions built on them), and the selection of one from a piece of a prompt. A module of its own, apart from the
prompt steps: they ask it for an operation, it answers with one entry of the catalog, or that none is supported.
Design: vault 02_projects/pilotstore/operation-selection-implementation.

    inventory.py   phase 1: SQLGlot's expressions, each tried on SQLite (what the engine really runs)
    catalog.py     phase 2: operations.db, the catalog shipped with the CLI (its single source), read-only at run time
    builders.py    phase 2: the registered builders, an entry's resolved parameters → our AST
    index.py       phase 3: each entry's vectors (in the catalog), the matrix, retrieve(text, k)
    evaluation.py  phases 3–4: held-out requests, Recall@K, the selector's choices (the developer's: models, paid)
    selector.py    phase 4: Jev chooses among the search's top 10, or none (unsupported)
"""
