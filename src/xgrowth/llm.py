"""Thin Claude wrapper: prompt caching on the stable system prompt + cost logging.

The client is injectable so tests can pass a fake without an API key or network.
"""

from __future__ import annotations

import sqlite3
from typing import Protocol

from . import cost


class LLMClient(Protocol):
    """Minimal surface the rest of the app depends on (easy to fake in tests)."""

    def complete(self, *, model: str, system: str, user: str, max_tokens: int = 1024) -> str:
        ...


class AnthropicClient:
    """Real client. Imports the SDK lazily so the package loads without it."""

    def __init__(self, api_key: str, conn: sqlite3.Connection | None = None) -> None:
        import anthropic  # lazy: keeps test import light

        self._client = anthropic.Anthropic(api_key=api_key)
        self._conn = conn

    def complete(self, *, model: str, system: str, user: str, max_tokens: int = 1024) -> str:
        # cache_control on the stable system block: the voice/style guide is reused
        # across every draft, so caching it cuts cost substantially.
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        if self._conn is not None:
            usage = resp.usage
            cost.record_claude(
                self._conn,
                model,
                input_tokens=getattr(usage, "input_tokens", 0),
                output_tokens=getattr(usage, "output_tokens", 0),
                cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            )
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
