"""Shared fixtures and fakes for the test suite (no network, no API keys)."""

from __future__ import annotations

import json

import pytest

from xgrowth import db
from xgrowth.config import config_from_dict
from xgrowth.git_watcher import ClassifyResult
from xgrowth.github_client import Commit


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
