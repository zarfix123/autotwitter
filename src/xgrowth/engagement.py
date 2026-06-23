"""Engagement safety core — the ONLY path to reply-to-others / follow.

Compliance contract (see plan "The safety contract"):
  * This is the only module that imports/calls tweepy engagement endpoints
    (reply to others, ``follow_user``).
  * ``engagement_gate`` is the only caller of ``XEngager`` methods, and it acts
    only after fully validating a single-use, item-bound, human-minted token.
  * ``mint_approval_token`` is the only token source and refuses any non
    allow-listed Telegram user. It is called from exactly one place: the Telegram
    approve-callback handler.

If you are adding an engagement capability, it goes here, behind the gate. There
is deliberately no other route to X engagement anywhere in the codebase.
"""

from __future__ import annotations

import secrets
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from . import audit, cost
from .config import Config


# --- engagement surface (instantiated/used only by the gate wiring) ----------
class XEngager(Protocol):
    def reply_to(self, text: str, target_tweet_id: str) -> str: ...
    def follow(self, handle: str) -> bool: ...


class DryRunXEngager:
    """No-op engager for tests/dry-run. Records calls; never hits the network."""

    def __init__(self) -> None:
        self.replies: list[tuple[str, str]] = []
        self.follows: list[str] = []
        self._counter = 0

    def reply_to(self, text: str, target_tweet_id: str) -> str:
        self._counter += 1
        self.replies.append((text, target_tweet_id))
        return f"dryrun-reply-{self._counter}"

    def follow(self, handle: str) -> bool:
        self.follows.append(handle)
        return True


class RealXEngager:
    """tweepy-backed engagement (OAuth 2.0 user token, scope tweet.write/follows).

    This class is the single home of the reply-to-others and follow endpoints.
    Accepts a static token or a provider callable; the tweepy client is rebuilt when
    a refreshed token arrives so approved engagements survive token expiry.
    """

    def __init__(self, token_source) -> None:
        self._provider = token_source if callable(token_source) else (lambda: token_source)
        self._token: str | None = None
        self._client = None

    def _c(self):
        import tweepy  # lazy

        tok = self._provider()
        if tok != self._token or self._client is None:
            self._token = tok
            self._client = tweepy.Client(bearer_token=tok, wait_on_rate_limit=True)
        return self._client

    def reply_to(self, text: str, target_tweet_id: str) -> str:
        resp = self._c().create_tweet(
            text=text, in_reply_to_tweet_id=target_tweet_id, user_auth=False
        )
        return str(resp.data["id"])

    def follow(self, handle: str) -> bool:
        client = self._c()
        user = client.get_user(username=handle, user_auth=False)
        if not user.data:
            return False
        resp = client.follow_user(target_user_id=user.data.id, user_auth=False)
        return bool(getattr(resp, "data", {}) and resp.data.get("following"))


# --- approval tokens ----------------------------------------------------------
@dataclass
class GateResult:
    ok: bool
    reason: str
    result_id: str | None = None


def mint_approval_token(
    conn: sqlite3.Connection,
    *,
    item_type: str,
    item_id: int,
    telegram_user_id: int,
    allowed_user_id: int | None,
    ttl_seconds: int = 300,
    now: datetime | None = None,
) -> str | None:
    """Create a single-use token bound to one item. The ONLY token source.

    Refuses unless the tap came from the allow-listed Telegram user. Returns the
    token string, or None if denied.
    """
    if allowed_user_id is None or telegram_user_id != allowed_user_id:
        audit.log(
            conn,
            "approval.mint_denied",
            entity_type=item_type,
            entity_id=item_id,
            detail={"telegram_user_id": telegram_user_id},
        )
        return None
    token = secrets.token_urlsafe(32)
    now = now or datetime.now(UTC)
    conn.execute(
        "INSERT INTO approvals(token, item_type, item_id, telegram_user_id, created_at, "
        "expires_at, used_at) VALUES(?,?,?,?,?,?,NULL)",
        (
            token,
            item_type,
            str(item_id),
            telegram_user_id,
            now.isoformat(),
            (now + timedelta(seconds=ttl_seconds)).isoformat(),
        ),
    )
    conn.commit()
    audit.log(
        conn, "approval.minted", entity_type=item_type, entity_id=item_id,
        detail={"telegram_user_id": telegram_user_id},
    )
    return token


def _reject(conn: sqlite3.Connection, action: str, item_id: int, reason: str) -> GateResult:
    audit.log(
        conn, "engagement.rejected", entity_type=action, entity_id=item_id,
        detail={"reason": reason},
    )
    return GateResult(ok=False, reason=reason)


def _follow_counts(conn: sqlite3.Connection, now: datetime) -> tuple[int, datetime | None]:
    """(# follows used today, last follow used_at) from consumed follow approvals."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    rows = conn.execute(
        "SELECT used_at FROM approvals WHERE item_type='follow' AND used_at IS NOT NULL "
        "AND used_at >= ?",
        (day_start,),
    ).fetchall()
    times = [datetime.fromisoformat(r["used_at"]) for r in rows]
    return len(times), (max(times) if times else None)


def engagement_gate(
    conn: sqlite3.Connection,
    engager: XEngager,
    action: str,
    item_id: int,
    token: str,
    *,
    allowed_user_id: int | None,
    config: Config,
    now: datetime | None = None,
) -> GateResult:
    """The ONLY path that performs reply-to-others / follow. Validates the token,
    enforces follow caps/spacing, performs exactly one engagement, consumes the
    token, and audits. Any validation failure → no X call.
    """
    now = now or datetime.now(UTC)

    if action not in ("reply", "follow"):
        return _reject(conn, action, item_id, "unknown_action")

    row = conn.execute("SELECT * FROM approvals WHERE token = ?", (token,)).fetchone()
    if row is None:
        return _reject(conn, action, item_id, "no_token")
    if row["item_type"] != action:
        return _reject(conn, action, item_id, "wrong_type")
    if row["item_id"] != str(item_id):
        return _reject(conn, action, item_id, "wrong_item")
    if row["used_at"] is not None:
        return _reject(conn, action, item_id, "used")
    if allowed_user_id is None or row["telegram_user_id"] != allowed_user_id:
        return _reject(conn, action, item_id, "wrong_user")
    try:
        if datetime.fromisoformat(row["expires_at"]) < now:
            return _reject(conn, action, item_id, "expired")
    except (TypeError, ValueError):
        return _reject(conn, action, item_id, "expired")

    # Follow-specific pacing/caps, enforced HERE (not just in the UI).
    if action == "follow":
        if config.max_follows_per_day <= 0:
            return _reject(conn, action, item_id, "follow_disabled")
        used_today, last_follow = _follow_counts(conn, now)
        if used_today >= config.max_follows_per_day:
            return _reject(conn, action, item_id, "follow_cap")
        if last_follow is not None and (now - last_follow) < timedelta(
            minutes=config.follow_min_spacing_minutes
        ):
            return _reject(conn, action, item_id, "follow_spacing")

    # Atomically consume the token (single use, race-safe).
    consumed = conn.execute(
        "UPDATE approvals SET used_at = ? WHERE token = ? AND used_at IS NULL",
        (now.isoformat(), token),
    )
    conn.commit()
    if consumed.rowcount != 1:
        return _reject(conn, action, item_id, "used")

    # Perform exactly one engagement (the only call site of engager methods).
    try:
        if action == "reply":
            result_id = _perform_reply(conn, engager, item_id)
        else:
            result_id = _perform_follow(conn, engager, item_id)
    except Exception as exc:  # noqa: BLE001
        audit.log(
            conn, "engagement.failed", entity_type=action, entity_id=item_id,
            detail={"error": str(exc)},
        )
        return GateResult(ok=False, reason="engager_error")

    audit.log(
        conn, "engagement.performed", entity_type=action, entity_id=item_id,
        detail={"result_id": result_id},
    )
    return GateResult(ok=True, reason="ok", result_id=result_id)


def _perform_reply(conn: sqlite3.Connection, engager: XEngager, draft_id: int) -> str | None:
    row = conn.execute(
        "SELECT rd.id, rd.text, ro.target_tweet_id FROM reply_drafts rd "
        "JOIN reply_opportunities ro ON ro.id = rd.opportunity_id WHERE rd.id = ?",
        (draft_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"reply_draft {draft_id} not found")
    reply_id = engager.reply_to(row["text"], row["target_tweet_id"])
    cost.record_x(conn, "post_create")
    conn.execute(
        "UPDATE reply_drafts SET status='sent', sent_tweet_id=? WHERE id=?",
        (reply_id, draft_id),
    )
    conn.execute(
        "UPDATE reply_opportunities SET status='sent' WHERE id=("
        "SELECT opportunity_id FROM reply_drafts WHERE id=?)",
        (draft_id,),
    )
    conn.commit()
    return reply_id


def _perform_follow(conn: sqlite3.Connection, engager: XEngager, candidate_id: int) -> str | None:
    row = conn.execute(
        "SELECT handle FROM follow_candidates WHERE id = ?", (candidate_id,)
    ).fetchone()
    if row is None:
        raise ValueError(f"follow_candidate {candidate_id} not found")
    ok = engager.follow(row["handle"])
    cost.record_x(conn, "follow")
    conn.execute(
        "UPDATE follow_candidates SET status=? WHERE id=?",
        ("done" if ok else "failed", candidate_id),
    )
    conn.commit()
    return row["handle"] if ok else None
