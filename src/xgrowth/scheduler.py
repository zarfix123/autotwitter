"""Scheduler: assign drafts to active posting windows.

Constraints (all hard):
  - at most ``posts_per_day`` per calendar day,
  - at least ``min_post_spacing_minutes`` between consecutive posts,
  - send times land inside a configured window, with +/- jitter so cadence is not
    mechanically regular.

``now`` and the jitter function are injectable for deterministic tests.
"""

from __future__ import annotations

import random
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from . import audit
from .config import Config

JitterFn = Callable[[int, int], int]  # (low, high) -> minutes


@dataclass
class Window:
    start: time
    end: time


@dataclass
class Slot:
    when: datetime
    window_end: datetime


def parse_windows(specs: list[str]) -> list[Window]:
    windows: list[Window] = []
    for spec in specs:
        start_s, end_s = spec.split("-")
        sh, sm = (int(x) for x in start_s.split(":"))
        eh, em = (int(x) for x in end_s.split(":"))
        windows.append(Window(start=time(sh, sm), end=time(eh, em)))
    return windows


def _day_anchors(day: date, windows: list[Window], config: Config, tzinfo) -> list[Slot]:
    """Up to posts_per_day anchor slots for one day, respecting in-window spacing."""
    anchors: list[Slot] = []
    spacing = timedelta(minutes=config.min_post_spacing_minutes)
    for w in windows:
        if len(anchors) >= config.posts_per_day:
            break
        start_dt = datetime.combine(day, w.start, tzinfo=tzinfo)
        end_dt = datetime.combine(day, w.end, tzinfo=tzinfo)
        cursor = start_dt
        while cursor <= end_dt and len(anchors) < config.posts_per_day:
            anchors.append(Slot(when=cursor, window_end=end_dt))
            cursor = cursor + spacing
    return anchors


def _existing_times(conn: sqlite3.Connection) -> list[datetime]:
    """All scheduled/posted draft times (to enforce caps + spacing across runs)."""
    rows = conn.execute(
        "SELECT scheduled_at FROM drafts "
        "WHERE status IN ('scheduled', 'posted') AND scheduled_at IS NOT NULL"
    ).fetchall()
    out: list[datetime] = []
    for r in rows:
        try:
            out.append(datetime.fromisoformat(r["scheduled_at"]))
        except (TypeError, ValueError):
            continue
    return out


def _pending_drafts(conn: sqlite3.Connection) -> list[int]:
    rows = conn.execute(
        "SELECT id FROM drafts WHERE status = 'draft' ORDER BY id"
    ).fetchall()
    return [r["id"] for r in rows]


def schedule_pending(
    conn: sqlite3.Connection,
    config: Config,
    *,
    now: datetime,
    jitter_fn: JitterFn | None = None,
    horizon_days: int = 14,
) -> list[tuple[int, datetime]]:
    """Assign every 'draft'-status row a future send time. Returns (draft_id, when)."""
    jitter_fn = jitter_fn or random.randint
    windows = parse_windows(config.posting_windows)
    if not windows:
        return []

    spacing = timedelta(minutes=config.min_post_spacing_minutes)
    tzinfo = now.tzinfo

    existing = _existing_times(conn)
    per_day: dict[date, int] = {}
    for t in existing:
        per_day[t.date()] = per_day.get(t.date(), 0) + 1
    last_time: datetime | None = max(existing) if existing else None

    # Build the chronological list of candidate anchor slots across the horizon.
    candidates: list[Slot] = []
    for offset in range(horizon_days):
        day = (now + timedelta(days=offset)).date()
        candidates.extend(_day_anchors(day, windows, config, tzinfo))
    candidates.sort(key=lambda s: s.when)

    assigned: list[tuple[int, datetime]] = []
    for draft_id in _pending_drafts(conn):
        chosen: datetime | None = None
        for slot in candidates:
            day = slot.when.date()
            if per_day.get(day, 0) >= config.posts_per_day:
                continue

            when = slot.when
            # Respect spacing from the previously assigned/known post.
            if last_time is not None:
                earliest = last_time + spacing
                if when < earliest:
                    if earliest > slot.window_end:
                        continue  # spacing pushes us out of this window
                    when = earliest
            if when <= now:
                if now >= slot.window_end:
                    continue
                when = now + timedelta(minutes=1)

            # Apply jitter, clamped to [when, window_end] and after now.
            jitter = jitter_fn(-config.post_jitter_minutes, config.post_jitter_minutes)
            jittered = when + timedelta(minutes=jitter)
            if jittered < when:
                jittered = when
            if jittered > slot.window_end:
                jittered = slot.window_end
            if jittered <= now:
                jittered = when

            chosen = jittered
            per_day[day] = per_day.get(day, 0) + 1
            last_time = chosen
            break

        if chosen is None:
            break  # horizon exhausted; remaining drafts wait for the next run

        conn.execute(
            "UPDATE drafts SET status = 'scheduled', scheduled_at = ? WHERE id = ?",
            (chosen.isoformat(), draft_id),
        )
        conn.commit()
        audit.log(
            conn,
            "draft.scheduled",
            entity_type="draft",
            entity_id=draft_id,
            detail={"scheduled_at": chosen.isoformat()},
        )
        assigned.append((draft_id, chosen))

    return assigned


def due_drafts(conn: sqlite3.Connection, *, now: datetime) -> list[int]:
    """Scheduled drafts whose send time has arrived."""
    rows = conn.execute(
        "SELECT id, scheduled_at FROM drafts WHERE status = 'scheduled' ORDER BY scheduled_at"
    ).fetchall()
    due: list[int] = []
    for r in rows:
        try:
            when = datetime.fromisoformat(r["scheduled_at"])
        except (TypeError, ValueError):
            continue
        if when <= now:
            due.append(r["id"])
    return due
