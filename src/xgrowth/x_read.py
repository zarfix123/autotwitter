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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from . import cost

# A static token string or a provider that returns a current token (auto-refresh).
TokenSource = Callable[[], str] | str
# Returns a fresh DB connection. The reader runs inside scheduler worker threads, so
# it must NOT hold a connection created elsewhere — SQLite forbids cross-thread use.
ConnFactory = Callable[[], sqlite3.Connection]


@dataclass
class Tweet:
    id: str
    author_handle: str
    author_id: str
    text: str
    created_at: str  # ISO 8601
    author_followers: int | None = None


@dataclass
class Metrics:
    impressions: int = 0
    likes: int = 0
    reposts: int = 0
    replies: int = 0


class XReader(Protocol):
    def search_recent(self, query: str, max_results: int = 10) -> list[Tweet]: ...
    def user_recent(self, handle: str, max_results: int = 5) -> list[Tweet]: ...
    def follower_counts(self, handles: list[str]) -> dict[str, int]: ...
    def tweet_metrics(self, tweet_ids: list[str]) -> dict[str, Metrics]: ...


class FakeXReader:
    """Deterministic reader for tests/dry-run. Returns preconfigured tweets."""

    def __init__(
        self,
        search_results: dict[str, list[Tweet]] | None = None,
        timelines: dict[str, list[Tweet]] | None = None,
        followers: dict[str, int] | None = None,
        metrics: dict[str, Metrics] | None = None,
    ) -> None:
        self.search_results = search_results or {}
        self.timelines = timelines or {}
        self.followers = followers or {}
        self.metrics = metrics or {}
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

    def tweet_metrics(self, tweet_ids: list[str]) -> dict[str, Metrics]:
        self.calls.append(("metrics", ",".join(tweet_ids)))
        return {tid: self.metrics.get(tid, Metrics()) for tid in tweet_ids}


class RealXReader:
    """tweepy-backed read-only client (OAuth 2.0 bearer / user token)."""

    def __init__(self, token_source: TokenSource, conn_factory: ConnFactory | None = None) -> None:
        self._provider = token_source if callable(token_source) else (lambda: token_source)
        self._token: str | None = None
        self._client = None
        self._conn_factory = conn_factory

    def _c(self):
        """Current tweepy client, rebuilt when the provider returns a refreshed token."""
        import tweepy  # lazy

        tok = self._provider()
        if tok != self._token or self._client is None:
            self._token = tok
            self._client = tweepy.Client(bearer_token=tok, wait_on_rate_limit=True)
        return self._client

    def _record(self, op: str, count: int) -> None:
        # Open a short-lived connection per call: the reader is shared across worker
        # threads, so it cannot reuse a connection bound to the thread that built it.
        if self._conn_factory is not None and count:
            conn = self._conn_factory()
            try:
                cost.record_x(conn, op, count)
            finally:
                conn.close()

    def search_recent(self, query: str, max_results: int = 10) -> list[Tweet]:
        resp = self._c().search_recent_tweets(
            query=query,
            max_results=max(10, min(max_results, 100)),
            tweet_fields=["created_at", "author_id"],
            expansions=["author_id"],
            user_fields=["username", "public_metrics"],
            user_auth=False,
        )
        return self._to_tweets(resp)

    def user_recent(self, handle: str, max_results: int = 5) -> list[Tweet]:
        user = self._c().get_user(username=handle, user_auth=False)
        self._record("user_read", 1)
        if not user.data:
            return []
        resp = self._c().get_users_tweets(
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
        resp = self._c().get_users(
            usernames=handles, user_fields=["public_metrics"], user_auth=False
        )
        self._record("user_read", len(handles))
        out: dict[str, int] = {}
        for u in resp.data or []:
            pm = getattr(u, "public_metrics", None) or {}
            out[u.username] = int(pm.get("followers_count", 0))
        return out

    def tweet_metrics(self, tweet_ids: list[str]) -> dict[str, Metrics]:
        out: dict[str, Metrics] = {}
        for i in range(0, len(tweet_ids), 100):  # API caps at 100 ids/call
            batch = tweet_ids[i : i + 100]
            resp = self._c().get_tweets(
                ids=batch, tweet_fields=["public_metrics"], user_auth=False
            )
            self._record("owned_read", len(batch))
            for t in resp.data or []:
                pm = getattr(t, "public_metrics", None) or {}
                out[str(t.id)] = Metrics(
                    impressions=int(pm.get("impression_count", 0)),
                    likes=int(pm.get("like_count", 0)),
                    reposts=int(pm.get("retweet_count", 0)),
                    replies=int(pm.get("reply_count", 0)),
                )
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
