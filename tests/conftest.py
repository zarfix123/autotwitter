"""Shared fixtures and fakes for the test suite (no network, no API keys)."""

from __future__ import annotations

import json

import pytest

from xgrowth import db
from xgrowth.config import config_from_dict
from xgrowth.git_watcher import ClassifyResult
from xgrowth.github_client import Commit
from xgrowth.news_source import NewsItem


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    db.init_db(c)
    yield c
    c.close()


@pytest.fixture
def config():
    return config_from_dict(
        {
            "repos": [{"owner": "zarfix123", "name": "autotwitter"}],
            "github_author": "zarfix123",
            "topic_clusters": ["AI", "edtech", "building a startup in public"],
            "target_accounts": ["levelsio"],
            "keywords": ["build in public"],
            "voice_samples": ["shipped a thing today."],
            "posting_windows": ["09:00-11:00", "16:00-18:00"],
            "posts_per_day": 2,
            "min_post_spacing_minutes": 180,
            "post_jitter_minutes": 25,
            "daily_reply_queue_size": 5,
            "max_follows_per_day": 2,
            "x_premium": False,
            "weekly_cost_cap_usd": 15.0,
        }
    )


@pytest.fixture
def news_config():
    return config_from_dict(
        {
            "repos": [{"owner": "zarfix123", "name": "autotwitter"}],
            "github_author": "zarfix123",
            "topic_clusters": ["AI", "edtech"],
            "target_accounts": ["levelsio"],
            "keywords": ["build in public"],
            "voice_samples": ["shipped a thing today."],
            "posting_windows": ["09:00-11:00", "16:00-18:00"],
            "posts_per_day": 2,
            "weekly_cost_cap_usd": 15.0,
            "ai_news_enabled": True,
            "ai_news_interval_hours": 6,
            "ai_news_max_per_day": 1,
            "news_min_points": 50,
            "news_item_max_age_hours": 24,
            "ai_news_style": "mix",
        }
    )


class FakeLLM:
    """Routes by system prompt: classify -> JSON verdict, draft -> JSON body."""

    def __init__(self, *, meaningful: bool = True, body: str = "shipped a clean feature today"):
        self._meaningful = meaningful
        self._body = body
        self.calls: list[dict] = []

    def complete(self, *, model: str, system: str, user: str, max_tokens: int = 1024) -> str:
        self.calls.append({"model": model, "system": system, "user": user})
        if "meaningful" in system:
            return json.dumps({"meaningful": self._meaningful, "topic": "AI"})
        return json.dumps({"body": self._body})

    def complete_with_search(
        self, *, model: str, system: str, user: str, max_tokens: int = 1024, max_searches: int = 3
    ):
        # Records the call (so tie-in context can be asserted) and returns no citations.
        self.calls.append(
            {"model": model, "system": system, "user": user, "search": True}
        )
        return json.dumps({"body": self._body}), []


class FakeNewsSource:
    """Returns a fixed list of NewsItems; records fetch calls."""

    def __init__(self, items: list[NewsItem]):
        self.items = items
        self.calls: list[dict] = []

    def fetch(self, queries, *, since_iso=None):
        self.calls.append({"queries": list(queries), "since_iso": since_iso})
        return list(self.items)


class FakeCommitSource:
    """Returns a fixed commit list; records calls."""

    def __init__(self, commits: list[Commit]):
        self.commits = commits
        self.calls: list[dict] = []

    def list_commits(self, repo, since, author):
        self.calls.append({"repo": repo.full_name, "since": since, "author": author})
        return list(self.commits)


def always_meaningful(summary, clusters) -> ClassifyResult:
    return ClassifyResult(meaningful=True, topic=clusters[0] if clusters else "general", summary=summary)


@pytest.fixture
def fake_llm():
    return FakeLLM()


@pytest.fixture
def sample_news_items():
    # item_id "2"/"4" hash even -> tie_in candidate; odd -> opinion, under "mix".
    return [
        NewsItem(
            item_id="101",
            title="OpenAI ships a new agent framework",
            url="https://example.com/openai-agents",
            points=320,
            num_comments=180,
            item_created_at="2026-06-23T08:00:00+00:00",
            author="dang",
        ),
        NewsItem(
            item_id="102",
            title="A deep dive into LLM evaluation harnesses",
            url="https://example.com/llm-evals",
            points=210,
            num_comments=64,
            item_created_at="2026-06-23T07:00:00+00:00",
            author="pg",
        ),
        NewsItem(
            item_id="103",
            title="Show HN: my weekend gardening app",  # off-topic, low signal
            url="https://example.com/garden",
            points=12,
            num_comments=3,
            item_created_at="2026-06-23T06:00:00+00:00",
            author="someone",
        ),
    ]


@pytest.fixture
def sample_commits():
    return [
        Commit(
            sha="aaa111",
            message="add reply ranking engine\n\nranks by relevance",
            date="2026-06-22T10:00:00Z",
            author="zarfix123",
            files=["src/xgrowth/monitor.py"],
        ),
        Commit(
            sha="bbb222",
            message="wire up scheduler caps",
            date="2026-06-22T09:00:00Z",
            author="zarfix123",
            files=["src/xgrowth/scheduler.py"],
        ),
    ]
