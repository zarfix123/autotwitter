"""AI-news drafting: a meaningful news_item -> a drafted original post.

For the top unconsumed news items (within the daily sub-cap), Claude is asked —
with the web search tool — to ground the post in current, cited info and write
either a sharp opinion on the story or a tie-in relating the founder's recent work
to it. Drafts land in the shared ``drafts`` queue (git_event_id = NULL) and are
auto-posted by the existing scheduler/poster.

These are ORIGINAL posts via XPoster: this module imports no engagement code, so
the static guardrail stays intact. The link-in-first-reply rule is enforced here
exactly as in content_gen.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from . import audit, cost, db
from .config import Config
from .content_gen import BodyContainsURLError, contains_url, strip_urls
from .llm import LLMClient
from .textfmt import max_len

__all__ = ["generate_news_drafts"]


def _news_drafted_today(conn: sqlite3.Connection, now: datetime) -> int:
    """Count AI-news drafts created today. News drafts have no git_event_id."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM drafts "
        "WHERE git_event_id IS NULL AND date(created_at) = date(?)",
        (now.isoformat(),),
    ).fetchone()
    return int(row["n"])


def _recent_work(conn: sqlite3.Connection, limit: int = 3) -> list[str]:
    """Recent meaningful commit summaries, for tie-in context (not consumed)."""
    rows = conn.execute(
        "SELECT summary FROM git_events WHERE is_meaningful = 1 "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["summary"] for r in rows if r["summary"]]


def _pick_style(config: Config, item_id: str, has_recent_work: bool) -> str:
    """opinion | tie_in. 'mix' alternates deterministically; tie_in needs real work."""
    style = config.ai_news_style
    if style == "mix":
        style = "tie_in" if (hash(item_id) % 2 == 0) else "opinion"
    if style == "tie_in" and not has_recent_work:
        return "opinion"  # a tie-in with nothing to tie to reads badly
    return style


def _system_prompt(config: Config, style: str, hints: str | None) -> str:
    samples = "\n".join(f"- {s}" for s in config.voice_samples)
    clusters = ", ".join(config.topic_clusters)
    perf = f"\nPerformance signal (use lightly, don't force): {hints}\n" if hints else ""
    if style == "tie_in":
        angle = (
            "Write ONE post that relates the founder's own recent work to this trending "
            "story — a genuine, specific connection, never forced or self-promotional."
        )
    else:
        angle = (
            "Write ONE sharp, specific opinion post reacting to this trending story — a "
            "real take with a point of view, not a summary or a hot-take cliché."
        )
    return (
        "You write original posts for a solo founder building in public in AI.\n"
        f"Topics: {clusters}.\n"
        "Voice samples (match this tone — direct, specific, lowercase-ok, no hype):\n"
        f"{samples}\n"
        f"{perf}\n"
        "You have the web search tool — search for current context on the story before "
        "writing so the take is grounded in what's actually happening.\n"
        f"{angle}\n"
        "Rules:\n"
        "- NEVER include a link or URL in the body. A link is added separately as a reply.\n"
        "- No hashtag spam, no 'excited to announce', no emojis unless natural.\n"
        '- Respond ONLY with compact JSON: {"body": str}.'
    )


def _user_prompt(title: str, points: int, recent: list[str] | None) -> str:
    parts = [f"Trending story: {title}", f"Hacker News score: {points}"]
    if recent:
        parts.append("\nThe founder's recent work:\n" + "\n".join(f"- {w}" for w in recent))
    parts.append("\nSearch the web to ground your take, then write the post.")
    return "\n".join(parts)


def _body_from_text(text: str) -> str:
    try:
        data = json.loads(text[text.index("{") : text.rindex("}") + 1])
        return str(data.get("body", "")).strip()
    except (ValueError, json.JSONDecodeError):
        return strip_urls(text).strip()


def generate_news_drafts(
    conn: sqlite3.Connection,
    config: Config,
    llm: LLMClient | None = None,
    *,
    now: datetime,
    hints: str | None = None,
) -> list[int]:
    """Draft posts for the top unconsumed news items, up to the daily sub-cap."""
    if not config.ai_news_enabled:
        return []
    if db.is_paused(conn):
        audit.log(conn, "news_draft.skipped", detail={"reason": "paused"})
        return []
    # Web search + drafting is the priciest call in the system — gate it on the cap.
    if cost.over_weekly_cap(conn, config.weekly_cost_cap_usd):
        audit.log(conn, "news_draft.skipped", detail={"reason": "weekly_cost_cap"})
        return []

    remaining = config.ai_news_max_per_day - _news_drafted_today(conn, now)
    if remaining <= 0:
        return []

    rows = conn.execute(
        "SELECT * FROM news_items WHERE is_meaningful = 1 AND consumed = 0 "
        "ORDER BY points DESC, item_created_at DESC"
    ).fetchall()

    limit = max_len(config.x_premium)
    recent = _recent_work(conn)
    created: list[int] = []
    for item in rows[:remaining]:
        style = _pick_style(config, item["item_id"], bool(recent))
        if llm is not None:
            text, _citations = llm.complete_with_search(
                model=config.models.draft,
                system=_system_prompt(config, style, hints),
                user=_user_prompt(
                    item["title"], item["points"], recent if style == "tie_in" else None
                ),
                max_tokens=600,
            )
            body = _body_from_text(text)
        else:
            body = item["title"].strip()  # offline fallback keeps the pipeline runnable

        if contains_url(body):
            body = strip_urls(body)
            if contains_url(body):
                raise BodyContainsURLError("generated news body still contains a URL after stripping")
        if len(body) > limit:
            body = body[:limit].rstrip()

        cur = conn.execute(
            "INSERT INTO drafts(git_event_id, kind, body, first_reply_link, status, model, "
            "topic, created_at) VALUES(NULL,?,?,?,?,?,?,?)",
            ("post", body, item["url"], "draft", config.models.draft, item["topic"], now.isoformat()),
        )
        conn.execute("UPDATE news_items SET consumed = 1 WHERE id = ?", (item["id"],))
        conn.commit()
        draft_id = int(cur.lastrowid)
        created.append(draft_id)
        audit.log(
            conn,
            "draft.created",
            entity_type="draft",
            entity_id=draft_id,
            detail={"source": "news", "news_item_id": item["id"], "style": style, "len": len(body)},
        )
    return created
