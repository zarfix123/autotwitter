"""X OAuth token lifecycle: refresh, rotation, persistence, graceful fallback.

All offline — the token endpoint is replaced by an injected ``post_fn``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from xgrowth import db, x_auth

NOW = datetime(2026, 6, 23, 9, 0, tzinfo=UTC)


class FakePost:
    """Stands in for the X token endpoint; records calls, returns a canned payload."""

    def __init__(self, payload=None, *, raises: Exception | None = None):
        self.payload = payload or {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 7200,
        }
        self.raises = raises
        self.calls: list[dict] = []

    def __call__(self, url, *, data, headers):
        self.calls.append({"url": url, "data": data, "headers": headers})
        if self.raises:
            raise self.raises
        return self.payload


@pytest.fixture
def db_factory(tmp_path):
    path = str(tmp_path / "x.db")
    c = db.connect(path)
    db.init_db(c)
    c.close()
    return lambda: db.connect(path)


def test_refresh_tokens_parses_and_rotates():
    post = FakePost({"access_token": "A2", "refresh_token": "R2", "expires_in": 1800})
    ts = x_auth.refresh_tokens("cid", "secret", "R1", now=NOW, post_fn=post)
    assert ts.access_token == "A2"
    assert ts.refresh_token == "R2"
    assert ts.expires_at == NOW + timedelta(seconds=1800)
    assert post.calls[0]["data"]["refresh_token"] == "R1"


def test_refresh_keeps_old_refresh_when_response_omits_it():
    post = FakePost({"access_token": "A2", "expires_in": 7200})
    ts = x_auth.refresh_tokens("cid", "secret", "R1", now=NOW, post_fn=post)
    assert ts.refresh_token == "R1"


def test_confidential_client_sends_basic_auth_public_does_not():
    conf = FakePost()
    x_auth.refresh_tokens("cid", "secret", "R1", now=NOW, post_fn=conf)
    assert conf.calls[0]["headers"].get("Authorization", "").startswith("Basic ")

    pub = FakePost()
    x_auth.refresh_tokens("cid", None, "R1", now=NOW, post_fn=pub)
    assert "Authorization" not in pub.calls[0]["headers"]


def test_provider_seeds_then_refreshes_and_caches(db_factory):
    post = FakePost()
    seed = x_auth.build_seed("env-access", "env-refresh", now=NOW)  # expired -> refresh on use
    provider = x_auth.XTokenProvider(
        client_id="cid", client_secret="secret", conn_factory=db_factory,
        seed=seed, now_fn=lambda: NOW, post_fn=post,
    )
    assert provider.token() == "new-access"   # refreshed
    assert len(post.calls) == 1
    assert provider.token() == "new-access"   # still valid -> no second refresh
    assert len(post.calls) == 1


def test_provider_without_refresh_uses_seed_token(db_factory):
    post = FakePost()
    seed = x_auth.build_seed("env-access", None, now=NOW)  # no refresh -> long-lived
    provider = x_auth.XTokenProvider(
        client_id="cid", client_secret="secret", conn_factory=db_factory,
        seed=seed, now_fn=lambda: NOW, post_fn=post,
    )
    assert provider.token() == "env-access"
    assert post.calls == []


def test_rotated_tokens_persist_across_provider_instances(db_factory):
    post = FakePost()
    seed = x_auth.build_seed("env-access", "env-refresh", now=NOW)
    first = x_auth.XTokenProvider(
        client_id="cid", client_secret="secret", conn_factory=db_factory,
        seed=seed, now_fn=lambda: NOW, post_fn=post,
    )
    assert first.token() == "new-access"

    # A fresh provider (restart) with no seed loads the persisted token, no refresh.
    post2 = FakePost()
    second = x_auth.XTokenProvider(
        client_id="cid", client_secret="secret", conn_factory=db_factory,
        seed=None, now_fn=lambda: NOW, post_fn=post2,
    )
    assert second.token() == "new-access"
    assert post2.calls == []


def test_refresh_failure_returns_last_token_and_audits(db_factory):
    post = FakePost(raises=RuntimeError("boom"))
    seed = x_auth.build_seed("env-access", "env-refresh", now=NOW)  # expired -> tries refresh
    provider = x_auth.XTokenProvider(
        client_id="cid", client_secret="secret", conn_factory=db_factory,
        seed=seed, now_fn=lambda: NOW, post_fn=post,
    )
    assert provider.token() == "env-access"  # falls back to the stale token, no crash
    conn = db_factory()
    try:
        events = [r["event"] for r in conn.execute("SELECT event FROM audit_log").fetchall()]
    finally:
        conn.close()
    assert "x_token.refresh_failed" in events
