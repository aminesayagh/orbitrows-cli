"""The single-file steps of the store import, in call order (one CSV file at a time).

    header.detect(jev, path)                       is the first row a header? (1 Jev yes/no)
    read_table(path, has_header=...)               orbitrows.services.csv.reader (code)
    types.classify(jev, table, ...)                SQLite type and nullability of every column (Jev); run without
                                                   context: {"context_columns": {}, "source_context": {}}
    profile.profile(table)                         counted facts about every column (code)
    context.classify(jev, table, schema, profile)  dedicated/header context of every column (Jev)
    roles.classify(jev, table, context, schema, profile)   role and level of every column (Jev)
"""
