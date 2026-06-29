"""Reply drafter: top-N queued opportunities -> URL-free, length-capped drafts."""

from __future__ import annotations

import json

from xgrowth import reply_drafter


class FakeReplyLLM:
    def __init__(self, reply: str = "have you tried scoping it down to one user first?"):
        self._reply = reply
        self.calls: list[dict] = []

    def complete(self, *, model, system, user, max_tokens=1024) -> str:
        self.calls.append({"system": system, "user": user})
        return json.dumps({"reply": self._reply})


def _opp(conn, rank, text="building in public is hard", handle="bob"):
    conn.execute(
        "INSERT INTO reply_opportunities(target_tweet_id, author_handle, text, "
        "relevance_score, rank, status, created_at) VALUES(?,?,?,?,?,'queued','t')",
        (f"t{rank}", handle, text, 0.9, rank),
    )
    conn.commit()


def test_draft_pending_drafts_top_n(conn, config):
    object.__setattr__(config, "daily_reply_queue_size", 2)
    for i in range(5):
        _opp(conn, i)
    drafts = reply_drafter.draft_pending(conn, config, FakeReplyLLM())
    assert len(drafts) == 2
    drafted = conn.execute(
        "SELECT COUNT(*) FROM reply_opportunities WHERE status='drafted'"
    ).fetchone()[0]
    assert drafted == 2


def test_reply_is_url_free(conn, config):
    _opp(conn, 0)
    reply_drafter.draft_pending(conn, config, FakeReplyLLM(reply="great, see https://spam.com/x"))
    text = conn.execute("SELECT text FROM reply_drafts").fetchone()["text"]
    from xgrowth.textfmt import contains_url

    assert not contains_url(text)


def test_reply_length_capped(conn, config):
    _opp(conn, 0)
    reply_drafter.draft_pending(conn, config, FakeReplyLLM(reply="x" * 500))
    text = conn.execute("SELECT text FROM reply_drafts").fetchone()["text"]
    assert len(text) <= 280


def test_recent_sent_replies_passed_to_model(conn, config):
    conn.execute(
        "INSERT INTO reply_drafts(opportunity_id, text, status, created_at) "
        "VALUES(NULL, 'a previously sent reply phrasing', 'sent', 't')"
    )
    conn.commit()
    _opp(conn, 0)
    llm = FakeReplyLLM()
    reply_drafter.draft_pending(conn, config, llm)
    assert "a previously sent reply phrasing" in llm.calls[0]["system"]


def test_fallback_without_llm(conn, config):
    _opp(conn, 0, text="shipping an edtech feature")
    drafts = reply_drafter.draft_pending(conn, config, llm=None)
    text = conn.execute("SELECT text FROM reply_drafts WHERE id=?", (drafts[0],)).fetchone()["text"]
    assert text  # non-empty
    assert len(text) <= 280


def test_only_queued_opportunities_drafted(conn, config):
    _opp(conn, 0)
    conn.execute("UPDATE reply_opportunities SET status='sent'")
    conn.commit()
    drafts = reply_drafter.draft_pending(conn, config, FakeReplyLLM())
    assert drafts == []


def test_performance_hint_reaches_prompt(conn, config):
    _opp(conn, 0)
    llm = FakeReplyLLM()
    reply_drafter.draft_pending(conn, config, llm, hints="replies to @levelsio land best")
    assert any("replies to @levelsio land best" in c["system"] for c in llm.calls)


def test_no_hint_no_performance_line(conn, config):
    _opp(conn, 0)
    llm = FakeReplyLLM()
    reply_drafter.draft_pending(conn, config, llm)
    assert all("What's been landing" not in c["system"] for c in llm.calls)
