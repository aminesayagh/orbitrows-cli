"""Classify one incoming CSV using the store import's existing steps.

The source is a snapshot: types.classify_incoming keeps every cell as TEXT.
Keys describe its records, but an unsafe primary key is not enforced on upload.
"""

from pathlib import Path

from orbitrows.pipeline import incoming_match
from orbitrows.pipeline.store import context, header, keys, profile, roles, run, types
from orbitrows.services.csv.reader import read_table


async def classify(model, path: Path, encoding: str | None = None, store_id: str | None = None) -> dict:
    saved, notices, ordered = {}, [], run._Ordered(model)

    def group(w):                                             # matching calls are independent: all asked at once
        return (w["step"], w["position"]) if w["step"] == "matching" else w["step"]

    def pause(phase: str):
        found = run._warnings(path.name, saved, "schema" if phase == "types" else phase)
        notices.extend(w for w in found if "question" not in w)
        if asked := run.first_questions(found, group, ordered.order):
            raise run.Paused(phase, asked, notices)

    saved["read"] = await header.detect(ordered, path, encoding=encoding)
    pause("header")
    table = read_table(path, has_header=saved["read"]["has_header"], encoding=encoding)
    saved["read"]["header"] = table["header"]
    saved["profile"] = profile.profile(table)
    saved["schema"] = types.classify_incoming(table)
    saved["context"] = await context.classify(ordered, table, saved["schema"], saved["profile"])
    pause("context")
    saved["roles"] = await roles.classify(ordered, table, saved["context"], saved["schema"], saved["profile"])
    pause("roles")
    result = await keys.classify(ordered, [(table, saved)])
    saved["schema"]["create_table"] = result[table["path"]]["create_table"]
    saved["keys"] = {"warnings": result[table["path"]]["warnings"]}
    pause("keys")
    if store_id is not None:
        saved["matching"] = await incoming_match.classify(ordered, table, saved, store_id)
        pause("matching")
    return {"table": table, "saved": saved, "notices": notices}
