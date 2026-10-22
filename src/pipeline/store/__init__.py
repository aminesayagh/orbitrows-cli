"""The steps of the store import. The first six run on one CSV file at a time; keys, joins and schema come after,
joins per file and keys and schema over all the store files together. run.py chains them, keys before joins.
Every step identifies a column by its position in the file (0 for the first), never by its header: two columns may
share a header. The header stays data: what Jev reads, what a warning shows ("column"), what the export writes back.

    header.detect(jev, path)                       is the first row a header? (1 Jev yes/no)
    read_table(path, has_header=...)               orbitrows.services.csv.reader (code)
    types.classify(jev, table, ...)                SQLite type and nullability of every column (Jev); run without
                                                   context: {"context_columns": {}, "source_context": {}}
    profile.profile(table)                         counted facts about every column (code)
    context.classify(jev, table, schema, profile)  dedicated/header context of every column (Jev)
    roles.classify(jev, table, context, schema, profile)   role and level of every column (Jev)
    joins.classify(jev, table, saved)              groups of columns that describe another record, per file (Jev + code)
    keys.classify(jev, [(table, saved), ...])      primary and foreign keys over all the store files (Jev + code)
    schema.assemble(files)                         the store schema, its metadata rows and export map (code)
    schema.round_trip(store, rows)                 proof: rebuild every file from the loaded rows (code); the import
                                                   itself proves it on disk, in storage.write
"""
