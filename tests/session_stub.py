"""The session server's handler in tests (ORBITROWS_HANDLER=session_stub:handle): the message is its SQL, one
request per statement (split on ;). No model."""


def handle(db, text: str, calls: dict) -> dict:
    return {"program": [{"request": f"r{i}", "ast": {"stub": i}, "sql": [sql.strip()]}
                        for i, sql in enumerate(text.split(";"), 1)]}


def handle_bytes(db, text: str, calls: dict) -> dict:
    """As handle, but each ast holds bytes: json.dumps refuses it once the change is applied."""
    return {"program": [p | {"ast": b"x"} for p in handle(db, text, calls)["program"]]}
