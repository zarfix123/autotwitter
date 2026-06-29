"""Content generator: body must be URL-free; link lives in a separate field."""

from __future__ import annotations

import pytest

from conftest import FakeLLM
from xgrowth import content_gen


def _make_event(conn, summary="- add reply ranking engine"):
    conn.execute(
        "INSERT INTO git_events(repo, commit_shas, summary, dedup_key, is_meaningful, "
        "topic, consumed, created_at) VALUES(?,?,?,?,1,?,0,?)",
        ("zarfix123/autotwitter", "[\"a\"]", summary, "k1", "AI", "2026-06-22T10:00:00Z"),
    )
    conn.commit()
    return conn.execute("SELECT id FROM git_events").fetchone()["id"]


def test_url_detection():
    assert content_gen.contains_url("check https://github.com/x/y")
    assert content_gen.contains_url("see github.com/foo/bar now")
    assert not content_gen.contains_url("shipped a clean feature today")


def test_draft_body_has_no_url_link_separate(conn, config, fake_llm):
    event_id = _make_event(conn)
    draft_id = content_gen.generate_draft(conn, event_id, config, fake_llm)
    row = conn.execute("SELECT * FROM drafts WHERE id = ?", (draft_id,)).fetchone()
    assert not content_gen.contains_url(row["body"])
    assert row["first_reply_link"] == "https://github.com/zarfix123/autotwitter"


def test_llm_url_in_body_is_stripped(conn, config):
    llm = FakeLLM(body="shipped it, see https://github.com/zarfix123/autotwitter")
    event_id = _make_event(conn)
    draft_id = content_gen.generate_draft(conn, event_id, config, llm)
    body = conn.execute("SELECT body FROM drafts WHERE id = ?", (draft_id,)).fetchone()["body"]
    assert not content_gen.contains_url(body)


def test_fallback_without_llm(conn, config):
    event_id = _make_event(conn, summary="- add reply ranking engine\nfiles: monitor.py")
    draft_id = content_gen.generate_draft(conn, event_id, config, llm=None)
    body = conn.execute("SELECT body FROM drafts WHERE id = ?", (draft_id,)).fetchone()["body"]
    assert "reply ranking engine" in body
    assert not content_gen.contains_url(body)


def test_body_truncated_to_standard_limit(conn, config):
    llm = FakeLLM(body="x" * 500)
    event_id = _make_event(conn)
    draft_id = content_gen.generate_draft(conn, event_id, config, llm)
    body = conn.execute("SELECT body FROM drafts WHERE id = ?", (draft_id,)).fetchone()["body"]
    assert len(body) <= 280


def test_event_marked_consumed_and_not_regenerated(conn, config, fake_llm):
    event_id = _make_event(conn)
    content_gen.generate_draft(conn, event_id, config, fake_llm)
    with pytest.raises(ValueError):
        content_gen.generate_draft(conn, event_id, config, fake_llm)


def test_hints_reach_system_prompt(conn, config, fake_llm):
    event_id = _make_event(conn)
    content_gen.generate_draft(conn, event_id, config, fake_llm, hints="top topics — AI")
    assert any("top topics — AI" in c["system"] for c in fake_llm.calls)


def test_no_hints_no_performance_line(conn, config, fake_llm):
    event_id = _make_event(conn)
    content_gen.generate_draft(conn, event_id, config, fake_llm)
    assert all("Performance signal" not in c["system"] for c in fake_llm.calls)


def test_generate_pending_only_meaningful_unconsumed(conn, config, fake_llm):
    _make_event(conn)  # meaningful, unconsumed
    conn.execute(
        "INSERT INTO git_events(repo, commit_shas, summary, dedup_key, is_meaningful, "
        "topic, consumed, created_at) VALUES('r','[]','s','k2',0,'AI',0,'t')"
    )  # not meaningful
    conn.commit()
    drafts = content_gen.generate_pending(conn, config, fake_llm)
    assert len(drafts) == 1


# --- windowed selector + de-dup -----------------------------------------------
def _ev(conn, summary, key, created="2026-06-22T10:00:00Z"):
    conn.execute(
        "INSERT INTO git_events(repo, commit_shas, summary, dedup_key, is_meaningful, "
        "topic, consumed, created_at) VALUES('r','[]',?,?,1,'AI',0,?)",
        (summary, key, created),
    )
    conn.commit()


def test_select_and_draft_picks_only_max_posts(conn, config, fake_llm):
    from datetime import UTC, datetime

    now = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
    _ev(conn, "- shipped feature A", "ka")
    _ev(conn, "- shipped feature B", "kb")
    ids = content_gen.select_and_draft(conn, config, fake_llm, now=now, max_posts=1)
    assert len(ids) == 1  # best one only, not every commit
    assert conn.execute("SELECT COUNT(*) FROM git_events WHERE consumed=1").fetchone()[0] == 1


def test_select_and_draft_excludes_stale_window(conn, config, fake_llm):
    from datetime import UTC, datetime

    now = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
    _ev(conn, "- old work", "kold", created="2026-06-01T10:00:00Z")  # > 7 days old
    ids = content_gen.select_and_draft(conn, config, fake_llm, now=now, max_posts=1)
    assert ids == []


def test_truncated_model_output_does_not_leak_json_wrapper(conn, config):
    class TruncLLM:  # simulates a too-long post cut off mid-JSON
        def complete(self, *, model, system, user, max_tokens=1024):
            return '{"body": "shipped Canvas LMS support today and then the post got cut'

    event_id = _make_event(conn)
    draft_id = content_gen.generate_draft(conn, event_id, config, TruncLLM())
    body = conn.execute("SELECT body FROM drafts WHERE id = ?", (draft_id,)).fetchone()["body"]
    assert body.startswith("shipped Canvas LMS support")
    assert "{" not in body and "body" not in body  # no {"body": wrapper in the tweet


def _ev_with_link(conn, link, key):
    conn.execute(
        "INSERT INTO git_events(repo, commit_shas, summary, dedup_key, is_meaningful, "
        "topic, consumed, link, created_at) VALUES('Hadeva-Dev/Tolus','[\"a\"]','- shipped X',?,1,'AI',0,?,'t')",
        (key, link),
    )
    conn.commit()
    return conn.execute("SELECT id FROM git_events ORDER BY id DESC LIMIT 1").fetchone()["id"]


def test_private_repo_event_drafts_with_no_link(conn, config, fake_llm):
    eid = _ev_with_link(conn, "", "kpriv")  # "" = private repo, no public URL
    draft_id = content_gen.generate_draft(conn, eid, config, fake_llm)
    link = conn.execute("SELECT first_reply_link FROM drafts WHERE id = ?", (draft_id,)).fetchone()["first_reply_link"]
    assert not link  # None/empty -> poster posts no self-reply


def test_event_links_to_homepage_when_set(conn, config, fake_llm):
    eid = _ev_with_link(conn, "https://tolus.dev", "khome")
    draft_id = content_gen.generate_draft(conn, eid, config, fake_llm)
    link = conn.execute("SELECT first_reply_link FROM drafts WHERE id = ?", (draft_id,)).fetchone()["first_reply_link"]
    assert link == "https://tolus.dev"


def test_select_and_draft_skips_near_duplicate(conn, config):
    from datetime import UTC, datetime

    now = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)
    conn.execute(
        "INSERT INTO drafts(kind, body, status, created_at) "
        "VALUES('post','shipped a clean feature today','posted','t')"
    )
    conn.commit()
    _ev(conn, "- shipped feature A", "ka")
    llm = FakeLLM(body="shipped a clean feature today")  # identical to the existing post
    ids = content_gen.select_and_draft(conn, config, llm, now=now, max_posts=1)
    assert ids == []  # near-duplicate -> skipped, not queued
