"""News watcher: discovery, dedup, classification, and the skip gates."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

from conftest import FakeNewsSource
from xgrowth import cost, db, news_watcher
from xgrowth.news_source import NewsItem
from xgrowth.news_watcher import news_heuristic_classifier

NOW = datetime(2026, 6, 23, 9, 0, tzinfo=UTC)


def test_scan_inserts_fresh_high_signal_items(conn, news_config, sample_news_items):
    src = FakeNewsSource(sample_news_items)
    ids = news_watcher.scan(conn, news_config, src, news_heuristic_classifier, now=NOW)
    rows = conn.execute("SELECT item_id, is_meaningful FROM news_items ORDER BY item_id").fetchall()
    # 101 + 102 pass the points floor and are AI-relevant; 103 is below the floor.
    assert {r["item_id"] for r in rows} == {"101", "102"}
    assert all(r["is_meaningful"] == 1 for r in rows)
    assert len(ids) == 2


def test_scan_dedups_on_second_run(conn, news_config, sample_news_items):
    src = FakeNewsSource(sample_news_items)
    news_watcher.scan(conn, news_config, src, news_heuristic_classifier, now=NOW)
    again = news_watcher.scan(conn, news_config, src, news_heuristic_classifier, now=NOW)
    assert again == []


def test_scan_skips_when_paused(conn, news_config, sample_news_items):
    db.set_paused(conn, True)
    src = FakeNewsSource(sample_news_items)
    assert news_watcher.scan(conn, news_config, src, news_heuristic_classifier, now=NOW) == []
    assert conn.execute("SELECT COUNT(*) AS n FROM news_items").fetchone()["n"] == 0


def test_scan_skips_over_cost_cap(conn, news_config, sample_news_items):
    cost.record_x(conn, "post_create_with_url")  # 0.20 >= 0.9 * cap below
    cfg = replace(news_config, weekly_cost_cap_usd=0.20)
    src = FakeNewsSource(sample_news_items)
    assert news_watcher.scan(conn, cfg, src, news_heuristic_classifier, now=NOW) == []


def test_scan_drops_stale_items(conn, news_config):
    stale = NewsItem(
        item_id="200",
        title="An old but high-scoring LLM story",
        url="https://example.com/old",
        points=500,
        item_created_at=(NOW - timedelta(hours=48)).isoformat(),
    )
    src = FakeNewsSource([stale])
    assert news_watcher.scan(conn, news_config, src, news_heuristic_classifier, now=NOW) == []
