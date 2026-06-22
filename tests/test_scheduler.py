"""Scheduler: caps, spacing, window membership, and jitter bounds."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from xgrowth import scheduler
from xgrowth.config import config_from_dict

NOW = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)  # before the first window


def _insert_drafts(conn, n):
    for _ in range(n):
        conn.execute(
            "INSERT INTO drafts(kind, body, first_reply_link, status, model, created_at) "
            "VALUES('post','body','https://github.com/x/y','draft','m','t')"
        )
    conn.commit()


def _in_a_window(dt, config):
    windows = scheduler.parse_windows(config.posting_windows)
    return any(w.start <= dt.time() <= w.end for w in windows)


def test_no_jitter_lands_on_window_anchors(conn, config):
    _insert_drafts(conn, 2)
    assigned = scheduler.schedule_pending(conn, config, now=NOW, jitter_fn=lambda a, b: 0)
    assert len(assigned) == 2
    t0, t1 = assigned[0][1], assigned[1][1]
    assert t0.time() == time(9, 0)
    assert t1.time() == time(16, 0)
    assert t0.date() == NOW.date()


def test_per_day_cap_enforced(conn, config):
    _insert_drafts(conn, 5)
    assigned = scheduler.schedule_pending(conn, config, now=NOW, jitter_fn=lambda a, b: 0)
    assert len(assigned) == 5
    per_day: dict = {}
    for _, when in assigned:
        per_day[when.date()] = per_day.get(when.date(), 0) + 1
    assert all(count <= config.posts_per_day for count in per_day.values())


def test_spacing_respected(conn, config):
    _insert_drafts(conn, 4)
    assigned = scheduler.schedule_pending(conn, config, now=NOW, jitter_fn=lambda a, b: 0)
    times = sorted(when for _, when in assigned)
    spacing = timedelta(minutes=config.min_post_spacing_minutes)
    for earlier, later in zip(times, times[1:], strict=False):
        assert later - earlier >= spacing


def test_jitter_stays_in_window(conn, config):
    _insert_drafts(conn, 2)
    # Max positive jitter must not push past the window end.
    assigned = scheduler.schedule_pending(
        conn, config, now=NOW, jitter_fn=lambda a, b: b  # always +jitter
    )
    for _, when in assigned:
        assert _in_a_window(when, config)


def test_all_assigned_in_future_and_marked_scheduled(conn, config):
    _insert_drafts(conn, 3)
    assigned = scheduler.schedule_pending(conn, config, now=NOW, jitter_fn=lambda a, b: 0)
    for draft_id, when in assigned:
        assert when > NOW
        status = conn.execute(
            "SELECT status FROM drafts WHERE id = ?", (draft_id,)
        ).fetchone()["status"]
        assert status == "scheduled"


def _three_window_one_post_config():
    return config_from_dict(
        {
            "repos": [],
            "github_author": "x",
            "topic_clusters": ["AI"],
            "target_accounts": [],
            "keywords": [],
            "voice_samples": [],
            "posting_windows": ["09:00-09:30", "12:00-12:30", "16:00-16:30"],
            "posts_per_day": 1,
            "min_post_spacing_minutes": 30,
            "post_jitter_minutes": 0,
        }
    )


def test_preferred_hours_biases_window_choice(conn):
    cfg = _three_window_one_post_config()
    _insert_drafts(conn, 1)
    assigned = scheduler.schedule_pending(
        conn, cfg, now=NOW, jitter_fn=lambda a, b: 0, preferred_hours=[16, 12, 9]
    )
    assert assigned[0][1].time() == time(16, 0)


def test_default_no_preference_picks_earliest(conn):
    cfg = _three_window_one_post_config()
    _insert_drafts(conn, 1)
    assigned = scheduler.schedule_pending(conn, cfg, now=NOW, jitter_fn=lambda a, b: 0)
    assert assigned[0][1].time() == time(9, 0)


def test_due_drafts_returns_only_past(conn, config):
    _insert_drafts(conn, 2)
    scheduler.schedule_pending(conn, config, now=NOW, jitter_fn=lambda a, b: 0)
    # Nothing due before the first window.
    assert scheduler.due_drafts(conn, now=NOW) == []
    # After both windows pass, both are due.
    later = NOW + timedelta(days=1)
    assert len(scheduler.due_drafts(conn, now=later)) == 2
