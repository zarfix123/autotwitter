"""X (Twitter) READ-ONLY surface.

Used by the monitor to find reply opportunities and follow candidates. It can
search recent posts, read user timelines, and look up follower counts — and
nothing else. It cannot post, reply, follow, like, repost, or DM. Engagement
lives only in ``engagement.py`` behind the human-approval gate.

Reads are metered via ``cost.record_x``. tweepy uses ``wait_on_rate_limit`` so the
rolling 15-minute windows are respected with backoff.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Protocol

from . import cost


@dataclass
class Tweet:
    id: str
    author_handle: str
    author_id: str
    text: str
    created_at: str  # ISO 8601
    author_followers: int | None = None


class XReader(Protocol):
    def search_recent(self, query: str, max_results: int = 10) -> list[Tweet]: ...
    def user_recent(self, handle: str, max_results: int = 5) -> list[Tweet]: ...
    def follower_counts(self, handles: list[str]) -> dict[str, int]: ...


class FakeXReader:
    """Deterministic reader for tests/dry-run. Returns preconfigured tweets."""

    def __init__(
        self,
        search_results: dict[str, list[Tweet]] | None = None,
        timelines: dict[str, list[Tweet]] | None = None,
        followers: dict[str, int] | None = None,
    ) -> None:
        self.search_results = search_results or {}
        self.timelines = timelines or {}
        self.followers = followers or {}
        self.calls: list[tuple[str, str]] = []

    def search_recent(self, query: str, max_results: int = 10) -> list[Tweet]:
        self.calls.append(("search", query))
        return list(self.search_results.get(query, []))[:max_results]

    def user_recent(self, handle: str, max_results: int = 5) -> list[Tweet]:
        self.calls.append(("timeline", handle))
        return list(self.timelines.get(handle, []))[:max_results]

    def follower_counts(self, handles: list[str]) -> dict[str, int]:
        self.calls.append(("followers", ",".join(handles)))
        return {h: self.followers.get(h, 0) for h in handles}


class RealXReader:
    """tweepy-backed read-only client (OAuth 2.0 bearer / user token)."""

    def __init__(self, token: str, conn: sqlite3.Connection | None = None) -> None:
        import tweepy  # lazy

        self._client = tweepy.Client(bearer_token=token, wait_on_rate_limit=True)
        self._conn = conn

    def _record(self, op: str, count: int) -> None:
        if self._conn is not None and count:
            cost.record_x(self._conn, op, count)

    def search_recent(self, query: str, max_results: int = 10) -> list[Tweet]:
        resp = self._client.search_recent_tweets(
            query=query,
            max_results=max(10, min(max_results, 100)),
            tweet_fields=["created_at", "author_id"],
            expansions=["author_id"],
            user_fields=["username", "public_metrics"],
            user_auth=False,
        )
        return self._to_tweets(resp)

    def user_recent(self, handle: str, max_results: int = 5) -> list[Tweet]:
        user = self._client.get_user(username=handle, user_auth=False)
        self._record("user_read", 1)
        if not user.data:
            return []
        resp = self._client.get_users_tweets(
            id=user.data.id,
            max_results=max(5, min(max_results, 100)),
            tweet_fields=["created_at", "author_id"],
            expansions=["author_id"],
            user_fields=["username", "public_metrics"],
            user_auth=False,
        )
        return self._to_tweets(resp)

    def follower_counts(self, handles: list[str]) -> dict[str, int]:
        if not handles:
            return {}
        resp = self._client.get_users(
            usernames=handles, user_fields=["public_metrics"], user_auth=False
        )
        self._record("user_read", len(handles))
        out: dict[str, int] = {}
        for u in resp.data or []:
            pm = getattr(u, "public_metrics", None) or {}
            out[u.username] = int(pm.get("followers_count", 0))
        return out

    def _to_tweets(self, resp) -> list[Tweet]:
        tweets = resp.data or []
        self._record("post_read", len(tweets))
        users = {u.id: u for u in (resp.includes or {}).get("users", [])}
        out: list[Tweet] = []
        for t in tweets:
            u = users.get(t.author_id)
            pm = getattr(u, "public_metrics", None) or {} if u else {}
            out.append(
                Tweet(
                    id=str(t.id),
                    author_handle=getattr(u, "username", "") if u else "",
                    author_id=str(t.author_id),
                    text=t.text,
                    created_at=t.created_at.isoformat() if t.created_at else "",
                    author_followers=int(pm.get("followers_count", 0)) if pm else None,
                )
            )
        return out
