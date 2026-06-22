"""Analytics: pull snapshots; insights compute top topics/hours from owned data."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xgrowth import analytics, cost, db
from xgrowth.x_read import FakeXReader, Metrics

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)


def _posted_original(conn, tweet_id, *, posted_at):
    conn.execute(
        "INSERT INTO posted_history(tweet_id, kind, draft_id, posted_at) "
        "VALUES(?, 'original', NULL, ?)",
        (tweet_id, posted_at),
    )
    conn.commit()


def _scored_post(conn, tweet_id, topic, hour, likes):
    """Seed a fully-attributed, scored original post (git_event→draft→history→analytics)."""
    conn.execute(
        "INSERT INTO git_events(repo, commit_shas, summary, dedup_key, is_meaningful, "
        "topic, consumed, created_at) VALUES('r','[]','s',?,1,?,1,'t')",
        (f"k-{tweet_id}", topic),
    )
    ge = conn.execute("SELECT id FROM git_events ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO drafts(git_event_id, kind, body, status, created_at) "
        "VALUES(?, 'post', 'b', 'posted', 't')",
        (ge,),
    )
    d = conn.execute("SELECT id FROM drafts ORDER BY id DESC LIMIT 1").fetchone()["id"]
    posted_at = f"2026-06-20T{hour:02d}:00:00+00:00"
    conn.execute(
        "INSERT INTO posted_history(tweet_id, kind, draft_id, posted_at) "
        "VALUES(?, 'original', ?, ?)",
        (tweet_id, d, posted_at),
    )
    conn.execute(
        "INSERT INTO analytics(tweet_id, impressions, likes, reposts, replies, fetched_at) "
        "VALUES(?, 1000, ?, 0, 0, ?)",
        (tweet_id, likes, NOW.isoformat()),
    )
    conn.commit()


def test_pull_writes_snapshots(conn, config):
    _posted_original(conn, "tw1", posted_at=(NOW - timedelta(days=1)).isoformat())
    _posted_original(conn, "tw2", posted_at=(NOW - timedelta(days=1)).isoformat())
    reader = FakeXReader(metrics={"tw1": Metrics(impressions=500, likes=10), "tw2": Metrics(likes=3)})
    snapped = analytics.pull(conn, config, reader, now=NOW)
    assert set(snapped) == {"tw1", "tw2"}
    rows = conn.execute("SELECT COUNT(*) FROM analytics").fetchone()[0]
    assert rows == 2


def test_pull_skips_when_paused(conn, config):
    _posted_original(conn, "tw1", posted_at=(NOW - timedelta(days=1)).isoformat())
    db.set_paused(conn, True)
    assert analytics.pull(conn, config, FakeXReader(metrics={"tw1": Metrics()}), now=NOW) == []


def test_pull_skips_over_cost_cap(conn, config):
    _posted_original(conn, "tw1", posted_at=(NOW - timedelta(days=1)).isoformat())
    for _ in range(10):
        cost.record_x(conn, "post_create_with_url")
    object.__setattr__(config, "weekly_cost_cap_usd", 1.0)
    assert analytics.pull(conn, config, FakeXReader(metrics={"tw1": Metrics()}), now=NOW) == []


def test_insights_empty_below_min_posts(conn):
    _scored_post(conn, "a", "AI", 9, 100)
    _scored_post(conn, "b", "AI", 9, 90)
    ins = analytics.insights(conn, min_posts=5)
    assert ins.top_topics == [] and ins.best_hours == [] and ins.hint_text == ""


def test_insights_ranks_topics_and_hours(conn):
    for i in range(3):
        _scored_post(conn, f"ai{i}", "AI", 9, 100)      # high score, hour 9
    for i in range(3):
        _scored_post(conn, f"ed{i}", "edtech", 16, 1)   # low score, hour 16
    ins = analytics.insights(conn, min_posts=5)
    assert ins.top_topics[0] == "AI"
    assert ins.best_hours[0] == 9
    assert "AI" in ins.hint_text and "09:00" in ins.hint_text
