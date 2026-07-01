"""Daily content-mix planner.

Decides the day's original posts as a *blend* of three categories instead of letting
each producer flood the queue:

  * commit  — about the founder's work (windowed best-pick from recent commits)
  * opinion — about the outside world (AI news)
  * tie_in  — the founder's work related to what's trending (the bridge)

It targets ``posts_per_day`` total, aims for the soft per-category split
(``commit_posts_per_day`` + ``ai_news_max_per_day``), and falls back to filling any
empty slot from whichever side still has material — so it reliably hits the daily
target even on a quiet-commit or quiet-news day. Each producer de-dups internally,
so the same thing is never posted twice.
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

    # 1) targeted commit slot(s)
    commit_need = min(max(0, config.commit_posts_per_day - counts.get("commit", 0)), remaining)
    if commit_need > 0:
        ids = content_gen.select_and_draft(
            conn, config, llm, now=now, max_posts=commit_need, hints=commit_hints, voice=voice
        )
        created["commit"] += ids
        remaining -= len(ids)

    # 2) targeted outside-world slot(s) (opinion / tie_in)
    news_have = counts.get("opinion", 0) + counts.get("tie_in", 0)
    news_need = min(max(0, config.ai_news_max_per_day - news_have), remaining)
    if news_need > 0:
        ids = news_content_gen.generate_news_drafts(
            conn, config, llm, now=now, hints=news_hints, voice=voice, limit=news_need
        )
        created["news"] += ids
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
