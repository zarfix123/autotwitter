"""Engagement gate: the token-validation matrix and follow caps/spacing.

The gate must NEVER call the engager without a valid, fresh, item-bound,
human-minted token.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xgrowth import engagement
from xgrowth.engagement import DryRunXEngager, engagement_gate, mint_approval_token

ALLOWED = 42
NOW = datetime(2026, 6, 22, 12, 0, tzinfo=UTC)


def _reply_draft(conn, target="t123"):
    conn.execute(
        "INSERT INTO reply_opportunities(target_tweet_id, author_handle, text, status, "
        "created_at) VALUES(?, 'bob', 'original post', 'drafted', 't')",
        (target,),
    )
    opp_id = conn.execute("SELECT id FROM reply_opportunities ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO reply_drafts(opportunity_id, text, status, created_at) "
        "VALUES(?, 'a sharp specific reply', 'draft', 't')",
        (opp_id,),
    )
    return conn.execute("SELECT id FROM reply_drafts ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _follow_candidate(conn, handle="alice"):
    conn.execute(
        "INSERT INTO follow_candidates(handle, reason, score, status, created_at) "
        "VALUES(?, 'relevant', 0.7, 'queued', 't')",
        (handle,),
    )
    return conn.execute("SELECT id FROM follow_candidates ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _mint(conn, item_type, item_id, *, user=ALLOWED, allowed=ALLOWED, ttl=300, now=NOW):
    return mint_approval_token(
        conn, item_type=item_type, item_id=item_id,
        telegram_user_id=user, allowed_user_id=allowed, ttl_seconds=ttl, now=now,
    )


# ---- happy path -------------------------------------------------------------
def test_valid_reply_sends_once_and_consumes_token(conn, config):
    draft_id = _reply_draft(conn)
    token = _mint(conn, "reply", draft_id)
    engager = DryRunXEngager()
    result = engagement_gate(
        conn, engager, "reply", draft_id, token, allowed_user_id=ALLOWED, config=config, now=NOW
    )
    assert result.ok
    assert len(engager.replies) == 1
    assert engager.replies[0] == ("a sharp specific reply", "t123")
    row = conn.execute("SELECT status, sent_tweet_id FROM reply_drafts WHERE id = ?", (draft_id,)).fetchone()
    assert row["status"] == "sent" and row["sent_tweet_id"]
    used = conn.execute("SELECT used_at FROM approvals WHERE token = ?", (token,)).fetchone()["used_at"]
    assert used is not None


def test_reply_not_allowed_surfaces_clear_reason(conn, config):
    # X returns 403 when the author restricted replies (or the target was a retweet).
    # The gate must classify it as a clear, human message, not a cryptic engager_error.
    draft_id = _reply_draft(conn)
    token = _mint(conn, "reply", draft_id)

    class BlockedEngager:
        def reply_to(self, text, target_tweet_id):
            raise RuntimeError(
                "403 Forbidden\nReply to this conversation is not allowed because you "
                "have not been mentioned or otherwise engaged by the author."
            )

        def follow(self, handle):
            return True

    result = engagement_gate(
        conn, BlockedEngager(), "reply", draft_id, token,
        allowed_user_id=ALLOWED, config=config, now=NOW,
    )
    assert not result.ok
    assert "not allowed" in result.reason and result.reason != "engager_error"


# ---- token-validation matrix (each must NOT call the engager) ----------------
def test_no_token_rejected(conn, config):
    draft_id = _reply_draft(conn)
    engager = DryRunXEngager()
    result = engagement_gate(
        conn, engager, "reply", draft_id, "bogus-token", allowed_user_id=ALLOWED, config=config, now=NOW
    )
    assert not result.ok and result.reason == "no_token"
    assert engager.replies == []


def test_wrong_item_rejected(conn, config):
    d1 = _reply_draft(conn)
    d2 = _reply_draft(conn, target="t999")
    token = _mint(conn, "reply", d1)
    engager = DryRunXEngager()
    result = engagement_gate(conn, engager, "reply", d2, token, allowed_user_id=ALLOWED, config=config, now=NOW)
    assert result.reason == "wrong_item"
    assert engager.replies == []


def test_wrong_type_rejected(conn, config):
    draft_id = _reply_draft(conn)
    token = _mint(conn, "reply", draft_id)
    engager = DryRunXEngager()
    result = engagement_gate(conn, engager, "follow", draft_id, token, allowed_user_id=ALLOWED, config=config, now=NOW)
    assert result.reason == "wrong_type"
    assert engager.follows == []


def test_wrong_user_rejected(conn, config):
    draft_id = _reply_draft(conn)
    token = _mint(conn, "reply", draft_id)  # minted for ALLOWED
    engager = DryRunXEngager()
    result = engagement_gate(conn, engager, "reply", draft_id, token, allowed_user_id=999, config=config, now=NOW)
    assert result.reason == "wrong_user"
    assert engager.replies == []


def test_expired_rejected(conn, config):
    draft_id = _reply_draft(conn)
    token = _mint(conn, "reply", draft_id, ttl=-1)  # already expired
    engager = DryRunXEngager()
    result = engagement_gate(conn, engager, "reply", draft_id, token, allowed_user_id=ALLOWED, config=config, now=NOW)
    assert result.reason == "expired"
    assert engager.replies == []


def test_reused_token_rejected(conn, config):
    draft_id = _reply_draft(conn)
    token = _mint(conn, "reply", draft_id)
    engager = DryRunXEngager()
    first = engagement_gate(conn, engager, "reply", draft_id, token, allowed_user_id=ALLOWED, config=config, now=NOW)
    second = engagement_gate(conn, engager, "reply", draft_id, token, allowed_user_id=ALLOWED, config=config, now=NOW)
    assert first.ok and not second.ok and second.reason == "used"
    assert len(engager.replies) == 1  # only the first one sent


# ---- mint refuses non-allow-listed user -------------------------------------
def test_mint_denied_for_non_allowlisted_user(conn):
    draft_id = _reply_draft(conn)
    token = _mint(conn, "reply", draft_id, user=7, allowed=ALLOWED)
    assert token is None
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


# ---- follow caps & spacing (enforced in the gate) ---------------------------
def test_follow_daily_cap_enforced(conn, config):
    object.__setattr__(config, "max_follows_per_day", 1)
    engager = DryRunXEngager()
    c1 = _follow_candidate(conn, "alice")
    t1 = _mint(conn, "follow", c1)
    r1 = engagement_gate(conn, engager, "follow", c1, t1, allowed_user_id=ALLOWED, config=config, now=NOW)
    c2 = _follow_candidate(conn, "carol")
    later = NOW + timedelta(hours=5)
    t2 = _mint(conn, "follow", c2, now=later)
    r2 = engagement_gate(conn, engager, "follow", c2, t2, allowed_user_id=ALLOWED, config=config, now=later)
    assert r1.ok and not r2.ok and r2.reason == "follow_cap"
    assert engager.follows == ["alice"]


def test_follow_spacing_enforced(conn, config):
    object.__setattr__(config, "max_follows_per_day", 5)
    object.__setattr__(config, "follow_min_spacing_minutes", 240)
    engager = DryRunXEngager()
    c1 = _follow_candidate(conn, "alice")
    t1 = _mint(conn, "follow", c1)
    engagement_gate(conn, engager, "follow", c1, t1, allowed_user_id=ALLOWED, config=config, now=NOW)
    c2 = _follow_candidate(conn, "carol")
    later = NOW + timedelta(minutes=10)
    t2 = _mint(conn, "follow", c2, now=later)
    r2 = engagement_gate(conn, engager, "follow", c2, t2, allowed_user_id=ALLOWED, config=config, now=later)
    assert r2.reason == "follow_spacing"
    assert engager.follows == ["alice"]


def test_follow_disabled_when_cap_zero(conn, config):
    object.__setattr__(config, "max_follows_per_day", 0)
    engager = DryRunXEngager()
    c1 = _follow_candidate(conn)
    token = _mint(conn, "follow", c1)
    result = engagement_gate(conn, engager, "follow", c1, token, allowed_user_id=ALLOWED, config=config, now=NOW)
    assert result.reason == "follow_disabled"
    assert engager.follows == []


def test_unknown_action_rejected(conn, config):
    engager = DryRunXEngager()
    result = engagement_gate(conn, engager, "like", 1, "tok", allowed_user_id=ALLOWED, config=config, now=NOW)
    assert result.reason == "unknown_action"


def test_module_exposes_only_engager_surface():
    # Defensive: the engager protocol is reply_to/follow only — no like/repost/dm.
    assert hasattr(engagement.DryRunXEngager, "reply_to")
    assert hasattr(engagement.DryRunXEngager, "follow")
    for forbidden in ("like", "repost", "retweet", "dm", "send_dm"):
        assert not hasattr(engagement.DryRunXEngager, forbidden)
