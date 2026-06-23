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

    def complete_with_search(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1024,
        max_searches: int = 3,
    ) -> tuple[str, list[dict]]:
        """Like ``complete`` but grounded in live web search. Returns (text, citations)."""
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
        self._record(model, resp.usage)
        return "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")

    def complete_with_search(
        self,
        *,
        model: str,
        system: str,
        user: str,
        max_tokens: int = 1024,
        max_searches: int = 3,
    ) -> tuple[str, list[dict]]:
        """Draft grounded in Claude's server-side web search (no beta header).

        The server runs its own tool loop; when it returns ``pause_turn`` we resend
        the accumulated turn to let it continue. Cost is recorded on every round
        plus a per-search fee for each web_search result block. Returns the joined
        text and a flat list of {url, title, cited_text} citations.
        """
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": max_searches}]
        messages: list[dict] = [{"role": "user", "content": user}]
        texts: list[str] = []
        citations: list[dict] = []
        searches = 0
        for _ in range(max_searches + 2):  # cap the pause_turn loop defensively
            resp = self._client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=messages,
                tools=tools,
            )
            self._record(model, resp.usage)
            for block in resp.content:
                btype = getattr(block, "type", None)
                if btype == "text":
                    texts.append(block.text)
                    for c in getattr(block, "citations", None) or []:
                        citations.append(
                            {
                                "url": getattr(c, "url", None),
                                "title": getattr(c, "title", None),
                                "cited_text": getattr(c, "cited_text", None),
                            }
                        )
                elif btype == "web_search_tool_result":
                    searches += 1
            if resp.stop_reason == "pause_turn":
                messages.append({"role": "assistant", "content": resp.content})
                continue
            break
        if searches and self._conn is not None:
            cost.record_search(self._conn, model, searches)
        return "".join(texts), citations

    def _record(self, model: str, usage) -> None:
        if self._conn is None:
            return
        cost.record_claude(
            self._conn,
            model,
            input_tokens=getattr(usage, "input_tokens", 0),
            output_tokens=getattr(usage, "output_tokens", 0),
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        )
