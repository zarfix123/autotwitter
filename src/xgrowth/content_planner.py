"""Daily content-mix planner.

Decides the day's original posts as a *blend* instead of letting each producer flood
the queue. The categories:

  * opinion — a pure take on trending AI news (the outside world)
  * commit  — about the founder's work (windowed best-pick from recent commits)
  * tie_in  — the founder's work related to what's trending (the bridge)

Shape of a day (targeting ``posts_per_day``):
  1. GUARANTEED: exactly one pure ``opinion`` AI-news post, always — never a tie-in.
  2. BEST-AVAILABLE: the remaining slot(s) prefer a ``commit`` post (your recent
     work); if there's no commit material they fall back to another AI-news post
     (``opinion``/``tie_in`` per ai_news_style).

So a normal day is 1 opinion + 1 commit; a no-commit day is 1 opinion + 1 more news.
Each producer de-dups internally, so the same thing is never posted twice.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from . import content_gen, news_content_gen
from .config import Config
from .llm import LLMClient


def _drafted_today(conn: sqlite3.Connection, now: datetime) -> dict[str, int]:
    rows = conn.execute(
        "SELECT category, COUNT(*) AS n FROM drafts "
        "WHERE date(created_at) = date(?) GROUP BY category",
        (now.isoformat(),),
    ).fetchall()
    return {r["category"]: r["n"] for r in rows if r["category"]}


def run(
    conn: sqlite3.Connection,
    config: Config,
    llm: LLMClient | None = None,
    *,
    now: datetime,
    voice: str = "",
    commit_hints: str | None = None,
    news_hints: str | None = None,
) -> dict[str, list[int]]:
    """Produce the day's mix of original posts, up to posts_per_day. Returns created ids."""
    created: dict[str, list[int]] = {"commit": [], "news": []}
    counts = _drafted_today(conn, now)
    remaining = config.posts_per_day - sum(counts.values())
    if remaining <= 0:
        return created

    # 1) GUARANTEED pure AI-news post — exactly one per day, always 'opinion' (never a
    #    tie-in), independent of what fills the other slot. force_style pins it.
    news_have = counts.get("opinion", 0) + counts.get("tie_in", 0)
    if news_have == 0 and config.ai_news_max_per_day > 0:
        ids = news_content_gen.generate_news_drafts(
            conn, config, llm, now=now, hints=news_hints, voice=voice,
            limit=1, force_style="opinion",
        )
        created["news"] += ids
        remaining -= len(ids)

    # 2) BEST-AVAILABLE slot(s) — prefer a commit post (the windowed best-pick of your
    #    recent work); if there's no commit material it falls through to another AI-news
    #    post (opinion/tie_in per ai_news_style) below.
    commit_need = min(max(0, config.commit_posts_per_day - counts.get("commit", 0)), remaining)
    if commit_need > 0:
        ids = content_gen.select_and_draft(
            conn, config, llm, now=now, max_posts=commit_need, hints=commit_hints, voice=voice
        )
        created["commit"] += ids
        remaining -= len(ids)

    # 3) fallback — fill leftover slots from whichever side still has material, so the
    #    day still targets posts_per_day even if one category came up empty. A category
    #    with a 0 per-day cap is treated as OFF and is never used to backfill (so e.g.
    #    commit_posts_per_day: 0 stays a pure no-commit day).
    while remaining > 0:
        before = remaining
        if config.ai_news_max_per_day > 0:
            n_ids = news_content_gen.generate_news_drafts(
                conn, config, llm, now=now, hints=news_hints, voice=voice, limit=1
            )
            created["news"] += n_ids
            remaining -= len(n_ids)
            if remaining <= 0:
                break
        if config.commit_posts_per_day > 0:
            c_ids = content_gen.select_and_draft(
                conn, config, llm, now=now, max_posts=1, hints=commit_hints, voice=voice
            )
            created["commit"] += c_ids
            remaining -= len(c_ids)
        if remaining == before:  # neither side produced anything new -> stop
            break
    return created
