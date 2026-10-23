"""Choose store and incoming sources before the store-only prompt compiler runs."""

import re
from pathlib import Path

from orbitrows.services.model.client import SURE, ask, call_id, choice, yes_no


def available(db) -> list[dict]:
    if not db.execute("SELECT 1 FROM sqlite_schema WHERE name = '_incoming_tables'").fetchone():
        return []
    rows = db.execute("SELECT id, sql_name, source_file FROM _incoming_tables ORDER BY id").fetchall()
    return [{"id": id, "sql_name": name, "source_file": file,
             "columns": [c for (c,) in db.execute(
                 "SELECT source_header FROM _incoming_columns WHERE table_id = ? ORDER BY position", (id,))]}
            for id, name, file in rows]


def named(prompt: str, name: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", prompt, re.IGNORECASE))


async def run(model, prompt: str, db) -> dict:
    """{store: bool, incoming: [table records], warnings: []}; no sources is an error."""
    tables = available(db)
    if not tables:
        path = next((Path(file) for _, schema, file in db.execute("PRAGMA database_list")
                     if schema == "main" and file), None)
        if path is not None and path.with_name("incoming.db").is_file():
            return {"store": False, "incoming": [], "warnings": [], "error":
                    "Incoming uploads from the previous storage layout need to be reimported before prompts run."}
        return {"store": True, "incoming": [], "warnings": []}
    state = {"prompt": prompt, "incoming_tables": tables}
    warnings = []
    explicitly = [t for t in tables if named(prompt, t["sql_name"])]
    if not explicitly:
        explicitly = [t for t in tables if named(prompt, Path(t["source_file"]).name)]
    selected = []
    if explicitly:
        by_file = {}
        for table in explicitly:
            by_file.setdefault(Path(table["source_file"]).name.casefold(), []).append(table)
        for group in by_file.values():
            if len(group) == 1:
                selected.extend(group)
                continue
            names = {str(t["id"]): f"{t['sql_name']} (upload {t['id']})" for t in group}
            key = "upload_" + str(group[0]["id"])
            prompt_question = {key: choice(
                "Select the uploaded CSV source for a merchant's request.",
                "Which upload of this filename does the prompt mean?", names,
                ["Ask the merchant when multiple uploads have the same filename. Do not choose a version by recency."])}
            recorded = getattr(getattr(model, "inner", model), "calls", {})
            already_asked = call_id(state, prompt_question) in recorded
            answers = await ask(model, state, prompt_question)
            answer, probabilities = answers[key]
            if not already_asked or probabilities.get(answer, 0) < 1.0:
                warnings.append({"problem": f"Several uploads of {Path(group[0]['source_file']).name} exist. Which one?",
                                 "question": {"type": "choice", "call": answers.call, "key": key,
                                              "options": [{"answer": label, "value": ident,
                                                           "probability": round(probabilities.get(ident, 0), 2)}
                                                          for ident, label in names.items()]}})
            selected.extend(t for t in group if str(t["id"]) == answer)
    else:
        questions = {f"incoming_{t['id']}": yes_no(
            "Choose the data sources needed to answer a merchant's request.",
            f"Does the request require incoming upload {t['sql_name']} from {Path(t['source_file']).name}?",
            ["Select an incoming upload only when its information is needed by this request.",
             "A request may need several incoming uploads and the store together.",
             "Do not infer an upload solely because its columns resemble store columns."])
            for t in tables}
        answers = await ask(model, state, questions)
        for table in tables:
            key = f"incoming_{table['id']}"
            p = answers.get(key, 0)
            if p >= 0.5:
                selected.append(table)
            if 1 - SURE < p < SURE:
                warnings.append({"problem": f"Does this request need {table['sql_name']}?",
                                 "probability": round(p, 2),
                                 "question": {"type": "yes_no", "call": answers.call, "key": key}})
    store_answer = await ask(model, state | {"selected_incoming": [t["sql_name"] for t in selected]},
                             {"store": yes_no("Choose the data sources needed to answer a merchant's request.",
                                               "Does this request need any data from the store tables?",
                                               ["A comparison or store update needs the store.",
                                                "A request only listing an incoming upload may not need the store."])})
    p = store_answer["store"]
    if 1 - SURE < p < SURE:
        warnings.append({"problem": "Does this request need the store?", "probability": round(p, 2),
                         "question": {"type": "yes_no", "call": store_answer.call, "key": "store"}})
    return {"store": p >= 0.5, "incoming": selected, "warnings": warnings}
