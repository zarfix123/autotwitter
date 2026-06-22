"""Poster: body-first then link reply, history + cost recorded, kill switch honored."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xgrowth import db
from xgrowth import poster as poster_mod
from xgrowth.x_client import DryRunXPoster

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)


def _scheduled_draft(conn, when, link="https://github.com/zarfix123/autotwitter"):
    conn.execute(
        "INSERT INTO drafts(kind, body, first_reply_link, status, scheduled_at, model, "
        "created_at) VALUES('post','shipped a feature today',?, 'scheduled', ?, 'm','t')",
        (link, when.isoformat()),
    )
    conn.commit()
    return conn.execute("SELECT id FROM drafts ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_publish_posts_body_then_link_reply(conn, config):
    draft_id = _scheduled_draft(conn, NOW - timedelta(minutes=5))
    poster = DryRunXPoster()
    published = poster_mod.publish_due(conn, config, poster, now=NOW)
    assert published == [draft_id]

    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    assert row["status"] == "posted"
    assert row["posted_tweet_id"]
    assert row["reply_tweet_id"]

    hist = conn.execute(
        "SELECT kind FROM posted_history ORDER BY kind"
    ).fetchall()
    kinds = {h["kind"] for h in hist}
    assert kinds == {"original", "self_reply_link"}
    # The body the poster actually tweeted must be the URL-free body, not the link.
    assert poster.created == ["shipped a feature today"]


def test_cost_recorded_for_post_and_link(conn, config):
    _scheduled_draft(conn, NOW - timedelta(minutes=5))
    poster_mod.publish_due(conn, config, DryRunXPoster(), now=NOW)
    ops = {r["op"] for r in conn.execute("SELECT op FROM api_usage").fetchall()}
    assert "post_create" in ops
    assert "post_create_with_url" in ops


def test_kill_switch_blocks_posting(conn, config):
    _scheduled_draft(conn, NOW - timedelta(minutes=5))
    db.set_paused(conn, True)
    published = poster_mod.publish_due(conn, config, DryRunXPoster(), now=NOW)
    assert published == []


def test_not_due_is_not_published(conn, config):
    _scheduled_draft(conn, NOW + timedelta(hours=2))
    published = poster_mod.publish_due(conn, config, DryRunXPoster(), now=NOW)
    assert published == []
