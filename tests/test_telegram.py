"""Telegram decision core: approve -> one gated send; skip -> none; stranger -> denied."""

from __future__ import annotations

from xgrowth import telegram_bot
from xgrowth.engagement import DryRunXEngager
from xgrowth.telegram_bot import handle_decision, make_callback, pending_items

ALLOWED = 42


def _reply_draft(conn, target="t1"):
    conn.execute(
        "INSERT INTO reply_opportunities(target_tweet_id, author_handle, text, status, "
        "created_at) VALUES(?, 'bob', 'a post', 'drafted', 't')",
        (target,),
    )
    opp = conn.execute("SELECT id FROM reply_opportunities ORDER BY id DESC LIMIT 1").fetchone()["id"]
    conn.execute(
        "INSERT INTO reply_drafts(opportunity_id, text, status, created_at) "
        "VALUES(?, 'a specific reply', 'draft', 't')",
        (opp,),
    )
    return conn.execute("SELECT id FROM reply_drafts ORDER BY id DESC LIMIT 1").fetchone()["id"]


def _follow(conn, handle="alice"):
    conn.execute(
        "INSERT INTO follow_candidates(handle, reason, score, status, created_at) "
        "VALUES(?, 'relevant', 0.8, 'queued', 't')",
        (handle,),
    )
    return conn.execute("SELECT id FROM follow_candidates ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_approve_reply_sends_once(conn, config):
    draft_id = _reply_draft(conn)
    engager = DryRunXEngager()
    msg = handle_decision(conn, config, ALLOWED, engager, ALLOWED, make_callback("approve", "reply", draft_id))
    assert msg == "✅ Sent."
    assert len(engager.replies) == 1
    assert conn.execute("SELECT status FROM reply_drafts WHERE id=?", (draft_id,)).fetchone()["status"] == "sent"


def test_skip_reply_sends_nothing(conn, config):
    draft_id = _reply_draft(conn)
    engager = DryRunXEngager()
    msg = handle_decision(conn, config, ALLOWED, engager, ALLOWED, make_callback("skip", "reply", draft_id))
    assert msg == "❌ Skipped."
    assert engager.replies == []
    assert conn.execute("SELECT status FROM reply_drafts WHERE id=?", (draft_id,)).fetchone()["status"] == "skipped"


def test_stranger_denied_no_mint_no_send(conn, config):
    draft_id = _reply_draft(conn)
    engager = DryRunXEngager()
    msg = handle_decision(conn, config, ALLOWED, engager, 999, make_callback("approve", "reply", draft_id))
    assert msg == "⛔ Not authorized."
    assert engager.replies == []
    assert conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0] == 0


def test_approve_follow_follows(conn, config):
    object.__setattr__(config, "max_follows_per_day", 2)
    cid = _follow(conn, "alice")
    engager = DryRunXEngager()
    msg = handle_decision(conn, config, ALLOWED, engager, ALLOWED, make_callback("approve", "follow", cid))
    assert msg == "✅ Followed."
    assert engager.follows == ["alice"]


def test_pending_items_lists_replies_and_follows(conn, config):
    object.__setattr__(config, "max_follows_per_day", 2)
    _reply_draft(conn)
    _follow(conn)
    items = pending_items(conn, config)
    assert len(items["replies"]) == 1
    assert len(items["follows"]) == 1


def test_callback_roundtrip_parse():
    assert telegram_bot.parse_callback(make_callback("approve", "reply", 12)) == ("approve", "reply", 12)


def test_help_text_lists_every_command():
    help_text = telegram_bot.HELP_TEXT
    for cmd in ("/status", "/queue", "/now", "/kill", "/resume", "/help"):
        assert cmd in help_text, f"{cmd} missing from /help"
    # mentions the approval model
    assert "Approve" in help_text
