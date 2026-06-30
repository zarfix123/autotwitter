"""Live-reply notifier: catch fresh posts from high-value targets and draft fast.

When a target account posts something brand-new, we draft a reply immediately so a
single timely reply can go out while the post is still climbing — pushed as a
one-tap Telegram approval (in app.py), outside the daily batch.

This module only reads + drafts. It imports no engagement code: a live reply still
goes out solely through the engagement gate after a human tap. The guardrail test
enforces that this file never references the gate, the engager, or token minting.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime

from . import audit, cost, db, reply_drafter
from .config import Config
from .llm import LLMClient
from .monitor import _age_minutes
from .x_read import XReader

logger = logging.getLogger(__name__)


def scan_live(
    conn: sqlite3.Connection,
    config: Config,
    reader: XReader,
    llm: LLMClient | None = None,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Draft replies for any fresh, unseen posts from target accounts.

    Returns the new reply_draft ids (to be pushed one-at-a-time). No engagement.
    """
    now = now or datetime.now(UTC)
    if db.is_paused(conn):
        audit.log(conn, "live_reply.skipped", detail={"reason": "paused"})
        return []
    if cost.over_weekly_cap(conn, config.weekly_cost_cap_usd):
        audit.log(conn, "live_reply.skipped", detail={"reason": "weekly_cost_cap"})
        return []

    existing = {
        r["target_tweet_id"]
        for r in conn.execute("SELECT target_tweet_id FROM reply_opportunities").fetchall()
    }

    drafted: list[int] = []
    for handle in config.target_accounts:
        try:
            latest = reader.user_recent(handle, max_results=1)
        except Exception:  # noqa: BLE001 — one bad/transient read must not sink the scan
            logger.warning("live_reply: read failed for @%s; skipping", handle, exc_info=True)
            continue
        if not latest:
            continue
        tweet = latest[0]

        age = _age_minutes(tweet.created_at, now)
        if age is None or age > config.live_reply_max_age_minutes:
            continue
        if tweet.id in existing:
            continue

        if config.live_reply_min_followers > 0:
            followers = tweet.author_followers
            if followers is None:
                followers = reader.follower_counts([handle]).get(handle, 0)
            if (followers or 0) < config.live_reply_min_followers:
                continue

        cur = conn.execute(
            "INSERT INTO reply_opportunities(target_tweet_id, author_handle, "
            "author_followers, text, posted_at, freshness_min, status, created_at) "
            "VALUES(?,?,?,?,?,?,'queued',?)",
            (
                tweet.id, tweet.author_handle, tweet.author_followers, tweet.text,
                tweet.created_at, age, now.isoformat(),
            ),
        )
        conn.commit()
        opp_id = int(cur.lastrowid)
        existing.add(tweet.id)

        draft_id = reply_drafter.draft_one(conn, opp_id, config, llm)
        drafted.append(draft_id)
        audit.log(
            conn, "live_reply.queued", entity_type="reply_draft", entity_id=draft_id,
            detail={"handle": handle, "age_min": round(age, 1)},
        )

    return drafted
