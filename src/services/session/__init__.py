"""A store's session: a background server per store holds one open transaction on its session.db, so a user's
changes can be undone until the session ends and is saved (server.py). The CLI's side is client.py."""
