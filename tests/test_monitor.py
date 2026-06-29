"""Monitor: ranks a fake timeline into opportunities; never engages."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from xgrowth import cost, db, monitor
from xgrowth.analytics import ReplyInsights
from xgrowth.x_read import FakeXReader, Tweet

NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)
FRESH = (NOW - timedelta(minutes=10)).isoformat()
STALE = (NOW - timedelta(minutes=600)).isoformat()


class FakeRankLLM:
    def __init__(self, relevance: float = 0.9):
        self._rel = relevance

    def complete(self, *, model, system, user, max_tokens=1024) -> str:
        return json.dumps([{"i": i, "relevance": self._rel} for i in range(20)])


def _reader(config):
    kw_query = f"{config.keywords[0]} -is:retweet -is:reply lang:en"
    return FakeXReader(
        timelines={
            "levelsio": [
                Tweet("t1", "levelsio", "u1", "shipping a new AI feature", FRESH, author_followers=50000)
            ]
        },
        search_results={
            kw_query: [
                Tweet("t2", "indiedev", "u2", "building in public is hard but worth it", FRESH, author_followers=1200)
            ]
        },
    )


def test_scan_creates_opportunities(conn, config):
    created = monitor.scan(conn, config, _reader(config), FakeRankLLM(), now=NOW)
    assert len(created) == 2
    rows = conn.execute("SELECT status FROM reply_opportunities").fetchall()
    assert all(r["status"] == "queued" for r in rows)


def test_scan_dedups_existing(conn, config):
    conn.execute(
        "INSERT INTO reply_opportunities(target_tweet_id, status, created_at) "
        "VALUES('t1','queued','t')"
    )
    conn.commit()
    created = monitor.scan(conn, config, _reader(config), FakeRankLLM(), now=NOW)
    # t1 already present -> only t2 added.
    assert len(created) == 1


def test_scan_skips_when_paused(conn, config):
    db.set_paused(conn, True)
    created = monitor.scan(conn, config, _reader(config), FakeRankLLM(), now=NOW)
    assert created == []
    assert conn.execute("SELECT COUNT(*) FROM reply_opportunities").fetchone()[0] == 0


def test_scan_skips_over_cost_cap(conn, config):
    # Drive weekly spend past the cap.
    for _ in range(10):
        cost.record_x(conn, "post_create_with_url")  # 0.20 each
    object.__setattr__(config, "weekly_cost_cap_usd", 1.0)
    created = monitor.scan(conn, config, _reader(config), FakeRankLLM(), now=NOW)
    assert created == []


def test_stale_posts_filtered(conn, config):
    reader = FakeXReader(
        timelines={"levelsio": [Tweet("old", "levelsio", "u", "old news", STALE, author_followers=100)]},
        search_results={},
    )
    object.__setattr__(config, "keywords", [])
    created = monitor.scan(conn, config, reader, FakeRankLLM(), now=NOW)
    assert created == []


def test_follow_candidates_derived_for_high_relevance(conn, config):
    object.__setattr__(config, "max_follows_per_day", 2)
    monitor.scan(conn, config, _reader(config), FakeRankLLM(relevance=0.9), now=NOW)
    follows = conn.execute("SELECT COUNT(*) FROM follow_candidates").fetchone()[0]
    assert follows >= 1


def test_no_follow_candidates_when_disabled(conn, config):
    object.__setattr__(config, "max_follows_per_day", 0)
    monitor.scan(conn, config, _reader(config), FakeRankLLM(relevance=0.9), now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM follow_candidates").fetchone()[0] == 0


def test_low_relevance_no_follow_candidates(conn, config):
    object.__setattr__(config, "max_follows_per_day", 2)
    monitor.scan(conn, config, _reader(config), FakeRankLLM(relevance=0.2), now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM follow_candidates").fetchone()[0] == 0


def test_opportunities_store_topic(conn, config):
    monitor.scan(conn, config, _reader(config), FakeRankLLM(), now=NOW)
    topics = [r["topic"] for r in conn.execute("SELECT topic FROM reply_opportunities").fetchall()]
    assert topics and all(t is not None for t in topics)  # default topic assigned


def test_reply_insights_rerank_by_author(conn, config):
    # Two equally-relevant, equally-fresh, equal-size posts; only the learned
    # author factor differs -> the high-performing author should rank first.
    object.__setattr__(config, "target_accounts", ["good", "bad"])
    object.__setattr__(config, "keywords", [])
    reader = FakeXReader(
        timelines={
            "good": [Tweet("g1", "good", "u", "a post", FRESH, author_followers=1000)],
            "bad": [Tweet("b1", "bad", "u", "a post", FRESH, author_followers=1000)],
        },
        search_results={},
    )
    ri = ReplyInsights(author_factors={"good": 2.0, "bad": 0.5})
    monitor.scan(conn, config, reader, FakeRankLLM(relevance=0.8), now=NOW, reply_insights=ri)
    ranks = {
        r["author_handle"]: r["rank"]
        for r in conn.execute("SELECT author_handle, rank FROM reply_opportunities").fetchall()
    }
    assert ranks["good"] < ranks["bad"]
