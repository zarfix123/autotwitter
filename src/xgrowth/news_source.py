"""AI-news discovery source.

Surfaces trending AI/tech stories so the bot can post a current opinion or tie its
own work to what's being discussed. Hacker News (via the free, unauthenticated
Algolia API) is the default: cheap, deterministic, and high signal for the
build-in-public niche. ``NewsSource`` is a Protocol so tests inject a fake.

This module only reads public story metadata — no engagement, no X writes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol


@dataclass
class NewsItem:
    item_id: str          # stable source id (HN objectID); the dedup key
    title: str
    url: str              # article URL (-> first_reply_link); falls back to the HN permalink
    points: int = 0
    num_comments: int = 0
    item_created_at: str | None = None  # ISO 8601, when the story was posted
    author: str = ""
    source: str = "hn"


class NewsSource(Protocol):
    def fetch(self, queries: list[str], *, since_iso: str | None = None) -> list[NewsItem]:
        ...


def _hn_item_from_hit(hit: dict) -> NewsItem:
    """Map one Algolia HN ``hit`` to a NewsItem. Pure (testable without network)."""
    object_id = str(hit.get("objectID", ""))
    permalink = f"https://news.ycombinator.com/item?id={object_id}"
    created_iso: str | None = None
    if hit.get("created_at"):
        created_iso = str(hit["created_at"])
    elif hit.get("created_at_i"):
        created_iso = datetime.fromtimestamp(int(hit["created_at_i"]), tz=UTC).isoformat()
    return NewsItem(
        item_id=object_id,
        title=str(hit.get("title") or "").strip(),
        url=str(hit.get("url") or permalink),
        points=int(hit.get("points") or 0),
        num_comments=int(hit.get("num_comments") or 0),
        item_created_at=created_iso,
        author=str(hit.get("author") or ""),
        source="hn",
    )


class HackerNewsSource:
    """Trending stories from Hacker News via the Algolia search API (no key needed)."""

    API = "https://hn.algolia.com/api/v1/search_by_date"

    def __init__(self, *, hits_per_page: int = 20) -> None:
        self._hits_per_page = hits_per_page

    def fetch(self, queries: list[str], *, since_iso: str | None = None) -> list[NewsItem]:
        import requests  # lazy: keeps the package importable without it

        since_epoch: int | None = None
        if since_iso:
            try:
                dt = datetime.fromisoformat(since_iso)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=UTC)
                since_epoch = int(dt.timestamp())
            except ValueError:
                since_epoch = None

        seen: dict[str, NewsItem] = {}
        for q in queries:
            params: dict[str, str] = {
                "query": q,
                "tags": "story",
                "hitsPerPage": str(self._hits_per_page),
            }
            if since_epoch is not None:
                params["numericFilters"] = f"created_at_i>{since_epoch}"
            resp = requests.get(self.API, params=params, timeout=30)
            resp.raise_for_status()
            for hit in resp.json().get("hits", []):
                item = _hn_item_from_hit(hit)
                if item.item_id and item.title:
                    seen.setdefault(item.item_id, item)
        return list(seen.values())
