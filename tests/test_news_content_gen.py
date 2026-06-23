"""AI-news drafting: URL-free body, sub-cap, style mix, shared draft queue."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from conftest import FakeLLM
from xgrowth import content_gen, news_content_gen
from xgrowth.scheduler import schedule_pending

NOW = datetime(2026, 6, 23, 9, 0, tzinfo=UTC)


def _insert_item(conn, *, item_id="101", title="OpenAI ships a new agent framework",
                 url="https://example.com/agents", points=300, topic="AI"):
    conn.execute(
        "INSERT INTO news_items(item_id, source, title, url, points, num_comments, topic, "
        "is_meaningful, consumed, item_created_at, created_at) VALUES(?,?,?,?,?,0,?,1,0,?,?)",
        (item_id, "hn", title, url, points, topic, "2026-06-23T08:00:00+00:00", NOW.isoformat()),
    )
    conn.commit()


def _insert_commit_event(conn, summary="- built the live scheduler"):
    conn.execute(
        "INSERT INTO git_events(repo, commit_shas, summary, dedup_key, is_meaningful, "
        "topic, consumed, created_at) VALUES('zarfix123/autotwitter','[\"a\"]',?,?,1,'AI',0,?)",
        (summary, f"k-{summary[:8]}", NOW.isoformat()),
    )
    conn.commit()
    return conn.execute("SELECT id FROM git_events ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_draft_url_free_with_link_and_topic(conn, news_config, fake_llm):
    _insert_item(conn)
    ids = news_content_gen.generate_news_drafts(conn, news_config, fake_llm, now=NOW)
    assert len(ids) == 1
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (ids[0],)).fetchone()
    assert not content_gen.contains_url(row["body"])
    assert row["first_reply_link"] == "https://example.com/agents"
    assert row["git_event_id"] is None
    assert row["topic"] == "AI"
    assert conn.execute("SELECT consumed FROM news_items").fetchone()["consumed"] == 1


def test_url_in_body_is_stripped(conn, news_config):
    _insert_item(conn)
    llm = FakeLLM(body="my take, read it https://x.ai/post now")
    ids = news_content_gen.generate_news_drafts(conn, news_config, llm, now=NOW)
    body = conn.execute("SELECT body FROM drafts WHERE id = ?", (ids[0],)).fetchone()["body"]
    assert not content_gen.contains_url(body)


def test_sub_cap_limits_drafts_per_day(conn, news_config, fake_llm):
    for i in range(3):
        _insert_item(conn, item_id=f"20{i}", url=f"https://example.com/{i}")
    ids = news_content_gen.generate_news_drafts(conn, news_config, fake_llm, now=NOW)
    assert len(ids) == 1  # ai_news_max_per_day = 1
    unconsumed = conn.execute("SELECT COUNT(*) AS n FROM news_items WHERE consumed = 0").fetchone()
    assert unconsumed["n"] == 2


def test_offline_uses_title(conn, news_config):
    _insert_item(conn, title="A grounded title with no url")
    ids = news_content_gen.generate_news_drafts(conn, news_config, llm=None, now=NOW)
    body = conn.execute("SELECT body FROM drafts WHERE id = ?", (ids[0],)).fetchone()["body"]
    assert body == "A grounded title with no url"


def test_tie_in_style_includes_recent_work(conn, news_config, fake_llm):
    _insert_commit_event(conn, summary="- built the live scheduler")
    _insert_item(conn)
    cfg = replace(news_config, ai_news_style="tie_in")
    news_content_gen.generate_news_drafts(conn, cfg, fake_llm, now=NOW)
    search_calls = [c for c in fake_llm.calls if c.get("search")]
    assert search_calls
    assert "built the live scheduler" in search_calls[-1]["user"]


def test_news_and_commit_drafts_share_the_queue(conn, news_config, fake_llm):
    event_id = _insert_commit_event(conn)
    content_gen.generate_draft(conn, event_id, news_config, fake_llm)
    _insert_item(conn)
    news_content_gen.generate_news_drafts(conn, news_config, fake_llm, now=NOW)

    scheduled = schedule_pending(conn, news_config, now=NOW)
    assert len(scheduled) == 2
    statuses = {r["status"] for r in conn.execute("SELECT status FROM drafts").fetchall()}
    assert statuses == {"scheduled"}
