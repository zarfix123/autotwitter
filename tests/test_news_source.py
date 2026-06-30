"""News source: pure Hacker News hit -> NewsItem mapping (no network)."""

from __future__ import annotations

from xgrowth.news_source import _hn_item_from_hit


def test_maps_full_hit():
    item = _hn_item_from_hit(
        {
            "objectID": "42",
            "title": "OpenAI ships a new agent framework",
            "url": "https://example.com/agents",
            "points": 321,
            "num_comments": 88,
            "created_at": "2026-06-23T08:00:00.000Z",
            "author": "dang",
        }
    )
    assert item.item_id == "42"
    assert item.url == "https://example.com/agents"
    assert item.points == 321
    assert item.num_comments == 88
    assert item.author == "dang"
    assert item.source == "hn"


def test_url_falls_back_to_hn_permalink():
    item = _hn_item_from_hit({"objectID": "99", "title": "Ask HN: how do you eval LLMs?"})
    assert item.url == "https://news.ycombinator.com/item?id=99"


def test_created_at_derived_from_epoch_when_iso_missing():
    item = _hn_item_from_hit(
        {"objectID": "7", "title": "x", "created_at_i": 1750665600}
    )
    assert item.item_created_at is not None
    assert item.item_created_at.startswith("2025-")  # parsed from the epoch


def test_uses_popularity_search_endpoint_not_newest_first():
    # Regression guard: 'search' (popularity-ranked) surfaces stories with real
    # traction so the points floor has candidates. 'search_by_date' returns newest-
    # first near-zero-point stories and nothing ever clears the floor.
    from xgrowth.news_source import HackerNewsSource

    assert HackerNewsSource.API.endswith("/search")
    assert "search_by_date" not in HackerNewsSource.API
