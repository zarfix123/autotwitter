"""Poster: publish due drafts as original tweets, then the link as a self-reply.

Order is always body-first (cheap $0.015 post, reach-optimized), then the link in
the first reply. Honors the kill-switch (paused) flag. Touches nothing but our own
content.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import audit, cost, db
from .config import Config
from .scheduler import due_drafts
from .x_client import XPoster


def _record_post(conn: sqlite3.Connection, tweet_id: str, kind: str, draft_id: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO posted_history(tweet_id, kind, draft_id, posted_at) "
        "VALUES(?,?,?,?)",
        (tweet_id, kind, draft_id, audit.now_iso()),
    )
    conn.commit()


def publish_draft(
    conn: sqlite3.Connection, draft_id: int, poster: XPoster
) -> tuple[str, str | None]:
    """Publish one draft. Returns (body_tweet_id, reply_tweet_id|None)."""
    row = conn.execute(
        "SELECT id, body, first_reply_link, status FROM drafts WHERE id = ?", (draft_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"draft {draft_id} not found")
    if row["status"] not in ("scheduled", "draft"):
        raise ValueError(f"draft {draft_id} has status {row['status']}, not publishable")

    # 1) Original body tweet ($0.015, no URL).
    body_tweet_id = poster.create_tweet(row["body"])
    cost.record_x(conn, "post_create")
    _record_post(conn, body_tweet_id, "original", draft_id)

    # 2) Link as the first reply to our own tweet (this reply carries the URL).
    reply_tweet_id: str | None = None
    link = row["first_reply_link"]
    if link:
        try:
            reply_tweet_id = poster.reply_to_own(link, body_tweet_id)
            cost.record_x(conn, "post_create_with_url")
            _record_post(conn, reply_tweet_id, "self_reply_link", draft_id)
        except Exception as exc:  # noqa: BLE001 - body already posted; log and continue
            audit.log(
                conn,
                "post.reply_failed",
                entity_type="draft",
                entity_id=draft_id,
                detail={"error": str(exc), "body_tweet_id": body_tweet_id},
            )

    conn.execute(
        "UPDATE drafts SET status = 'posted', posted_tweet_id = ?, reply_tweet_id = ? "
        "WHERE id = ?",
        (body_tweet_id, reply_tweet_id, draft_id),
    )
    conn.commit()
    audit.log(
        conn,
        "post.published",
        entity_type="draft",
        entity_id=draft_id,
        detail={"body_tweet_id": body_tweet_id, "reply_tweet_id": reply_tweet_id},
    )
    return body_tweet_id, reply_tweet_id


def publish_due(
    conn: sqlite3.Connection, config: Config, poster: XPoster, *, now: datetime
) -> list[int]:
    """Publish all drafts whose scheduled time has arrived. Returns published ids."""
    if db.is_paused(conn):
        audit.log(conn, "post.skipped_paused")
        return []

    published: list[int] = []
    for draft_id in due_drafts(conn, now=now):
        try:
            publish_draft(conn, draft_id, poster)
            published.append(draft_id)
        except Exception as exc:  # noqa: BLE001
            conn.execute("UPDATE drafts SET status = 'failed' WHERE id = ?", (draft_id,))
            conn.commit()
            audit.log(
                conn,
                "post.failed",
                entity_type="draft",
                entity_id=draft_id,
                detail={"error": str(exc)},
            )
    return published
