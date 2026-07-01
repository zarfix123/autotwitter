"""Content-mix planner: balanced daily blend, fallback fill, cross-category dedup."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime

from xgrowth import content_planner

NOW = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)


_COMMIT_BODIES = [
    "finally killed the flaky scheduler race condition today",
    "refactored the dedup layer and everything reads cleaner now",
]
_NEWS_BODIES = [
    "agents are eating traditional saas faster than people expected",
    "the real moat now is distribution, not raw model quality",
    "everyone underestimates how boring genuinely reliable systems must be",
]


class FakePlannerLLM:
    """Distinct wording per draft so legitimate posts aren't flagged as duplicates."""

    def __init__(self):
        self.c = 0
        self.w = 0

    def complete(self, *, model, system, user, max_tokens=1024):
        if "order" in system:  # commit ranking
            return json.dumps({"order": [0]})
        if "meaningful" in system:
            return json.dumps({"meaningful": True, "topic": "AI"})
        body = _COMMIT_BODIES[self.c % len(_COMMIT_BODIES)]
        self.c += 1
        return json.dumps({"body": body})

    def complete_with_search(self, *, model, system, user, max_tokens=1024, max_searches=3):
        body = _NEWS_BODIES[self.w % len(_NEWS_BODIES)]
        self.w += 1
        return json.dumps({"body": body}), []


def _commit_event(conn, summary, key):
    conn.execute(
        "INSERT INTO git_events(repo, commit_shas, summary, dedup_key, is_meaningful, topic, "
        "consumed, created_at) VALUES('zarfix123/autotwitter','[\"a\"]',?,?,1,'AI',0,?)",
        (summary, key, NOW.isoformat()),
    )
    conn.commit()


def _news_item(conn, *, item_id, url, title):
    conn.execute(
        "INSERT INTO news_items(item_id, source, title, url, points, num_comments, topic, "
        "is_meaningful, consumed, item_created_at, created_at) VALUES(?,?,?,?,?,0,?,1,0,?,?)",
        (item_id, "hn", title, url, 300, "AI", NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()


def test_mix_one_commit_one_news(conn, news_config):
    _commit_event(conn, "- shipped the live scheduler", "k1")
    _news_item(conn, item_id="101", url="https://e.com/a", title="OpenAI ships an agent framework")
    created = content_planner.run(conn, news_config, FakePlannerLLM(), now=NOW)
    assert len(created["commit"]) == 1
    assert len(created["news"]) == 1
    cats = sorted(r["category"] for r in conn.execute("SELECT category FROM drafts").fetchall())
    assert "commit" in cats
    assert any(c in ("opinion", "tie_in") for c in cats)
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 2  # = posts_per_day


def test_fallback_fills_to_target_when_no_commit(conn, news_config):
    # No commits this week; plenty of news -> both daily slots fill from news.
    for i in range(3):
        _news_item(conn, item_id=f"n{i}", url=f"https://e.com/{i}", title=f"AI agents story {i}")
    created = content_planner.run(conn, news_config, FakePlannerLLM(), now=NOW)
    assert created["commit"] == []
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 2


def test_respects_posts_per_day_already_met(conn, news_config):
    # Two drafts already created today -> planner adds nothing.
    for b in ("already one", "already two"):
        conn.execute(
            "INSERT INTO drafts(kind, body, status, category, created_at) "
            "VALUES('post',?,'scheduled','commit',?)",
            (b, NOW.isoformat()),
        )
    conn.commit()
    _commit_event(conn, "- more work", "k1")
    _news_item(conn, item_id="101", url="https://e.com/a", title="news")
    created = content_planner.run(conn, news_config, FakePlannerLLM(), now=NOW)
    assert created == {"commit": [], "news": []}
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 2


def _pure_news_config(news_config):
    # Locked testing config: exactly 1 pure AI-news opinion post/day, no commits.
    return replace(
        news_config, posts_per_day=1, commit_posts_per_day=0,
        ai_news_max_per_day=1, ai_news_style="opinion",
    )


def test_locked_pure_news_one_opinion_post(conn, news_config):
    cfg = _pure_news_config(news_config)
    _commit_event(conn, "- shipped the live scheduler", "k1")  # exists but must be ignored
    _news_item(conn, item_id="101", url="https://e.com/a", title="OpenAI ships an agent framework")
    created = content_planner.run(conn, cfg, FakePlannerLLM(), now=NOW)
    assert created["commit"] == []            # never a commit post
    assert len(created["news"]) == 1          # exactly one news post
    rows = conn.execute("SELECT category FROM drafts").fetchall()
    assert [r["category"] for r in rows] == ["opinion"]  # pure opinion, not tie_in/commit


def test_locked_pure_news_no_commit_fallback_when_news_dry(conn, news_config):
    # No news items available -> must NOT fall back to a commit post (stays pure/empty).
    cfg = _pure_news_config(news_config)
    _commit_event(conn, "- shipped the live scheduler", "k1")
    created = content_planner.run(conn, cfg, FakePlannerLLM(), now=NOW)
    assert created == {"commit": [], "news": []}
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0


def test_dedup_drops_identical_cross_category(conn, news_config):
    _commit_event(conn, "- shipped scheduler", "k1")
    _news_item(conn, item_id="101", url="https://e.com/a", title="news")

    class DupLLM:
        def complete(self, *, model, system, user, max_tokens=1024):
            if "order" in system:
                return json.dumps({"order": [0]})
            if "meaningful" in system:
                return json.dumps({"meaningful": True, "topic": "AI"})
            return json.dumps({"body": "one identical sentence used for every single post"})

        def complete_with_search(self, *, model, system, user, max_tokens=1024, max_searches=3):
            return json.dumps({"body": "one identical sentence used for every single post"}), []

    content_planner.run(conn, news_config, DupLLM(), now=NOW)
    # commit post lands; the news post is a near-dup of it -> skipped, can't be refilled.
    assert conn.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1
