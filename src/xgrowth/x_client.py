"""X (Twitter) write surface — ORIGINAL POSTING ONLY.

This module deliberately exposes only two operations:
  - create_tweet(text)            -> post original content
  - reply_to_own(text, our_id)    -> reply to one of OUR OWN tweets (the link reply)

It has no ability to reply to others, follow, like, repost, or DM. Those actions
exist only behind the Phase 2 engagement gate, which requires a per-item human
approval token. Keeping engagement endpoints out of this surface entirely is what
makes auto-engagement structurally impossible, not just disabled.

Auth is OAuth 2.0 user context (never password login, never browser automation).
"""

from __future__ import annotations

from typing import Protocol


class XPoster(Protocol):
    def create_tweet(self, text: str) -> str:
        """Post an original tweet; return its id."""
        ...

    def reply_to_own(self, text: str, our_tweet_id: str) -> str:
        """Reply to one of our own tweets; return the reply id."""
        ...


class DryRunXPoster:
    """No-op poster for dry runs / tests. Returns synthetic ids, never hits network."""

    def __init__(self) -> None:
        self.created: list[str] = []
        self._counter = 0

    def _next_id(self) -> str:
        self._counter += 1
        return f"dryrun-{self._counter}"

    def create_tweet(self, text: str) -> str:
        tid = self._next_id()
        self.created.append(text)
        return tid

    def reply_to_own(self, text: str, our_tweet_id: str) -> str:
        return self._next_id()


class RealXPoster:
    """tweepy-backed poster using an OAuth 2.0 user-context access token.

    The token must carry the ``tweet.write`` scope. Token refresh (OAuth 2.0
    tokens expire) is handled by the caller/runtime, not here.
    """

    def __init__(self, oauth2_user_access_token: str) -> None:
        import tweepy  # lazy

        # user_auth=False on calls => use this OAuth 2.0 user token as the bearer.
        self._client = tweepy.Client(bearer_token=oauth2_user_access_token)

    def create_tweet(self, text: str) -> str:
        resp = self._client.create_tweet(text=text, user_auth=False)
        return str(resp.data["id"])

    def reply_to_own(self, text: str, our_tweet_id: str) -> str:
        resp = self._client.create_tweet(
            text=text, in_reply_to_tweet_id=our_tweet_id, user_auth=False
        )
        return str(resp.data["id"])
