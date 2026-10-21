"""The session server's handler in tests (ORBITROWS_HANDLER=session_stub:handle): the message is its SQL, one
request per statement (split on ;). No model."""


def handle(db, text: str) -> dict:
    return {"program": [{"request": f"r{i}", "ast": {"stub": i}, "sql": [sql.strip()]}
                        for i, sql in enumerate(text.split(";"), 1)]}
