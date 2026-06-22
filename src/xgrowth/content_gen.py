"""Content generator: a meaningful git_event -> a drafted original post.

The link-in-first-reply rule is enforced at generation time: we emit ``body`` and
``first_reply_link`` as separate fields and assert the body contains no URL. A body
with a link in it costs ~13x more on the X API and loses 50-90% of reach.
"""

from __future__ import annotations

import json
import sqlite3

from . import audit
from .config import Config
from .llm import LLMClient
from .textfmt import contains_url, strip_urls

__all__ = ["contains_url", "strip_urls", "generate_draft", "generate_pending"]


class BodyContainsURLError(ValueError):
    """Raised if a generated post body contains a URL (links go in the first reply)."""


def repo_url(repo_full_name: str) -> str:
    return f"https://github.com/{repo_full_name}"


def _max_len(config: Config) -> int:
    from .textfmt import max_len

    return max_len(config.x_premium)


def _system_prompt(config: Config, hints: str | None = None) -> str:
    # Stable across drafts -> cached. Keep voice guide here.
    samples = "\n".join(f"- {s}" for s in config.voice_samples)
    clusters = ", ".join(config.topic_clusters)
    perf = f"\nPerformance signal (use lightly, don't force): {hints}\n" if hints else ""
    return (
        "You write original 'building in public' posts for a solo founder on X.\n"
        f"Topics: {clusters}.\n"
        "Voice samples (match this tone — direct, specific, lowercase-ok, no hype):\n"
        f"{samples}\n"
        f"{perf}\n"
        "Rules:\n"
        "- Write ONE post about what shipped and why it matters.\n"
        "- NEVER include a link or URL in the body. A link is added separately as a reply.\n"
        "- No hashtags spam, no 'excited to announce', no emojis unless natural.\n"
        '- Respond ONLY with compact JSON: {"body": str}.'
    )


def _fallback_body(summary: str, config: Config) -> str:
    """Offline body when no LLM is available (keeps the pipeline runnable in tests)."""
    first = next(
        (ln[2:] for ln in summary.splitlines() if ln.startswith("- ")), "shipped something"
    )
    body = f"shipped: {first}".strip()
    return body[: _max_len(config)]


def generate_draft(
    conn: sqlite3.Connection,
    event_id: int,
    config: Config,
    llm: LLMClient | None = None,
    *,
    first_reply_link: str | None = None,
    hints: str | None = None,
) -> int:
    """Generate a draft for a meaningful git_event. Returns the draft id."""
    row = conn.execute(
        "SELECT id, repo, summary, is_meaningful, consumed FROM git_events WHERE id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        raise ValueError(f"git_event {event_id} not found")
    if not row["is_meaningful"]:
        raise ValueError(f"git_event {event_id} is not marked meaningful")
    if row["consumed"]:
        raise ValueError(f"git_event {event_id} already consumed")

    summary = row["summary"] or ""

    if llm is not None:
        text = llm.complete(
            model=config.models.draft,
            system=_system_prompt(config, hints),
            user=f"Change summary:\n{summary}",
            max_tokens=400,
        )
        try:
            data = json.loads(text[text.index("{") : text.rindex("}") + 1])
            body = str(data.get("body", "")).strip()
        except (ValueError, json.JSONDecodeError):
            body = strip_urls(text).strip()
    else:
        body = _fallback_body(summary, config)

    # Enforce link-in-first-reply: the body must never contain a URL.
    if contains_url(body):
        body = strip_urls(body)
        if contains_url(body):
            raise BodyContainsURLError("generated body still contains a URL after stripping")

    max_len = _max_len(config)
    if len(body) > max_len:
        body = body[:max_len].rstrip()

    link = first_reply_link or repo_url(row["repo"])

    cur = conn.execute(
        "INSERT INTO drafts(git_event_id, kind, body, first_reply_link, status, model, "
        "created_at) VALUES(?,?,?,?,?,?,?)",
        (event_id, "post", body, link, "draft", config.models.draft, audit.now_iso()),
    )
    conn.execute("UPDATE git_events SET consumed = 1 WHERE id = ?", (event_id,))
    conn.commit()
    draft_id = int(cur.lastrowid)
    audit.log(
        conn,
        "draft.created",
        entity_type="draft",
        entity_id=draft_id,
        detail={"git_event_id": event_id, "len": len(body), "has_link": bool(link)},
    )
    return draft_id


def generate_pending(
    conn: sqlite3.Connection,
    config: Config,
    llm: LLMClient | None = None,
    *,
    hints: str | None = None,
) -> list[int]:
    """Generate drafts for all meaningful, unconsumed git_events."""
    rows = conn.execute(
        "SELECT id FROM git_events WHERE is_meaningful = 1 AND consumed = 0 ORDER BY id"
    ).fetchall()
    return [generate_draft(conn, r["id"], config, llm, hints=hints) for r in rows]
