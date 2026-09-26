"""Import pipeline, one module per stage (see docs/data-structures.md in the PoC).

1. context  - detect units/currency/locale per incoming column (Jev + Pint/Babel)
2. columns  - link incoming columns to store columns, classify role/level (Jev)
3. rows     - match incoming rows to store records (keys, fuzzy, embeddings, Jev ranking)
4. cells    - convert values and stage proposed updates (code only)
"""
