"""Jev client (TypeSafe System One), reached through OpenRouter."""

import os

from typesafe_sdk import AsyncTypeSafeClient

MODEL = "typesafe/jev-1.13"


def connect() -> AsyncTypeSafeClient:
    return AsyncTypeSafeClient(
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url="https://openrouter.ai/api",
        model=MODEL,
    )
