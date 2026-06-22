"""Live-reply: fresh target post -> drafted opportunity; never engages."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xgrowth import cost, db, live_reply
from xgrowth.x_read import FakeXReader, Tweet

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
FRESH = (NOW - timedelta(minutes=5)).isoformat()
STALE = (NOW - timedelta(minutes=60)).isoformat()


def _reader(created_at, followers=1000):
    return FakeXReader(
        timelines={"levelsio": [Tweet("lt1", "levelsio", "u1", "a fresh hot take", created_at, author_followers=followers)]}
    )


def test_fresh_post_creates_draft(conn, config):
    drafted = live_reply.scan_live(conn, config, _reader(FRESH), llm=None, now=NOW)
    assert len(drafted) == 1
    opp = conn.execute("SELECT status FROM reply_opportunities").fetchone()
    assert opp["status"] == "drafted"
    rd = conn.execute("SELECT status FROM reply_drafts").fetchone()
    assert rd["status"] == "draft"  # drafted only — NOT sent (approval still required)


def test_stale_post_ignored(conn, config):
    assert live_reply.scan_live(conn, config, _reader(STALE), llm=None, now=NOW) == []
    assert conn.execute("SELECT COUNT(*) FROM reply_opportunities").fetchone()[0] == 0


def test_dedup_existing_target(conn, config):
    conn.execute(
        "INSERT INTO reply_opportunities(target_tweet_id, status, created_at) "
        "VALUES('lt1','queued','t')"
    )
    conn.commit()
    assert live_reply.scan_live(conn, config, _reader(FRESH), llm=None, now=NOW) == []


def test_skips_when_paused(conn, config):
    db.set_paused(conn, True)
    assert live_reply.scan_live(conn, config, _reader(FRESH), llm=None, now=NOW) == []


def test_skips_over_cost_cap(conn, config):
    for _ in range(10):
        cost.record_x(conn, "post_create_with_url")
    object.__setattr__(config, "weekly_cost_cap_usd", 1.0)
    assert live_reply.scan_live(conn, config, _reader(FRESH), llm=None, now=NOW) == []


def test_follower_floor_filters(conn, config):
    object.__setattr__(config, "live_reply_min_followers", 5000)
    assert live_reply.scan_live(conn, config, _reader(FRESH, followers=100), llm=None, now=NOW) == []
