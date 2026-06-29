"""Content generator: a meaningful git_event -> a drafted original post.

The link-in-first-reply rule is enforced at generation time: we emit ``body`` and
``first_reply_link`` as separate fields and assert the body contains no URL. A body
with a link in it costs ~13x more on the X API and loses 50-90% of reach.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from . import audit, dedupe
from .config import Config
from .llm import LLMClient
from .textfmt import contains_url, extract_json_field, strip_urls

__all__ = [
    "contains_url",
    "strip_urls",
    "generate_draft",
    "generate_pending",
    "select_and_draft",
]


class BodyContainsURLError(ValueError):
    """Raised if a generated post body contains a URL (links go in the first reply)."""


def repo_url(repo_full_name: str) -> str:
    return f"https://github.com/{repo_full_name}"


def _max_len(config: Config) -> int:
    from .textfmt import max_len

    return max_len(config.x_premium)


def _voice_block(config: Config, voice: str) -> str:
    """Prefer the distilled blog voice; fall back to the static config samples."""
    if voice:
        return voice
    samples = "\n".join(f"- {s}" for s in config.voice_samples)
    return (
        "Voice samples (match this tone — direct, specific, lowercase-ok, no hype):\n"
        f"{samples}"
    )


def _avoid_block(recent: list[str] | None) -> str:
    if not recent:
        return ""
    shown = "\n".join(f"- {r}" for r in recent[:8])
    return (
        "\nAlready posted recently — do NOT repeat these angles, openers, or phrasing; "
        "if it's the same work, take a clearly different angle:\n" + shown + "\n"
    )


def _system_prompt(
    config: Config, hints: str | None = None, voice: str = "", recent: list[str] | None = None
) -> str:
    # Stable across drafts -> cached. Voice + recent-posts steer tone and de-dup.
    clusters = ", ".join(config.topic_clusters)
    perf = f"\nPerformance signal (use lightly, don't force): {hints}\n" if hints else ""
    limit = _max_len(config)
    return (
        "You write original 'building in public' posts for a solo founder on X.\n"
        f"Topics: {clusters}.\n"
        f"{_voice_block(config, voice)}\n"
        f"{perf}{_avoid_block(recent)}\n"
        "Rules:\n"
        "- Write ONE post about what shipped and why it matters.\n"
        f"- The ENTIRE post MUST be under {limit} characters and read as COMPLETE — a "
        "finished thought, never cut off mid-sentence. Write short; do not run long.\n"
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
    voice: str = "",
    recent: list[str] | None = None,
) -> int | None:
    """Generate a commit draft for a meaningful git_event.

    Returns the draft id, or None if it was skipped as a near-duplicate of a recent
    post (the event is marked consumed either way, so it isn't retried).
    """
    row = conn.execute(
        "SELECT id, repo, summary, is_meaningful, consumed, link FROM git_events WHERE id = ?",
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
            system=_system_prompt(config, hints, voice, recent),
            user=f"Change summary:\n{summary}",
            max_tokens=400,
        )
        body = extract_json_field(text, "body")
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

    # De-dup guard: never queue a near-copy of something already posted/queued.
    if recent and dedupe.too_similar(body, recent):
        conn.execute("UPDATE git_events SET consumed = 1 WHERE id = ?", (event_id,))
        conn.commit()
        audit.log(
            conn, "draft.skipped_duplicate", entity_type="git_event", entity_id=event_id,
            detail={"reason": "too_similar"},
        )
        return None

    # Link policy: prefer the link the watcher resolved for this repo — a public URL,
    # or "" meaning "private repo with no public homepage -> post no link at all".
    # Fall back to the GitHub URL only for legacy events that predate the link column.
    if first_reply_link is not None:
        link = first_reply_link
    elif row["link"] is not None:
        link = row["link"] or None
    else:
        link = repo_url(row["repo"])

    cur = conn.execute(
        "INSERT INTO drafts(git_event_id, kind, body, first_reply_link, status, model, "
        "category, created_at) VALUES(?,?,?,?,?,?,'commit',?)",
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
        detail={"git_event_id": event_id, "len": len(body), "has_link": bool(link),
                "category": "commit"},
    )
    return draft_id


def generate_pending(
    conn: sqlite3.Connection,
    config: Config,
    llm: LLMClient | None = None,
    *,
    hints: str | None = None,
) -> list[int]:
    """Legacy: draft EVERY meaningful, unconsumed git_event (one post per event).

    Superseded by ``select_and_draft`` (windowed best-pick) in the content planner;
    kept for the Phase-1 ``run_cycle`` path and its tests.
    """
    rows = conn.execute(
        "SELECT id FROM git_events WHERE is_meaningful = 1 AND consumed = 0 ORDER BY id"
    ).fetchall()
    out = [generate_draft(conn, r["id"], config, llm, hints=hints) for r in rows]
    return [d for d in out if d is not None]


def _rank_events(llm: LLMClient | None, config: Config, rows: list) -> list[int]:
    """Order candidate git_event ids best-first (most post-worthy). Newest-first fallback."""
    ids = [r["id"] for r in rows]
    if llm is None or len(rows) <= 1:
        return ids
    numbered = "\n".join(
        f'{i}. (topic: {r["topic"]}) {(r["summary"] or "").splitlines()[0][:200]}'
        for i, r in enumerate(rows)
    )
    system = (
        "You pick which recent 'building in public' work is most worth ONE post right "
        "now. Favor shipped features, milestones, and notable fixes with a clear story; "
        "rank minor chores last. Respond ONLY with compact JSON: "
        '{"order": [int, ...]} — candidate indices, best first.'
    )
    text = llm.complete(
        model=config.models.classify, system=system, user=f"Candidates:\n{numbered}", max_tokens=200
    )
    try:
        data = json.loads(text[text.index("{") : text.rindex("}") + 1])
        order = [ids[i] for i in data.get("order", []) if isinstance(i, int) and 0 <= i < len(ids)]
        for i in ids:  # append anything the model dropped
            if i not in order:
                order.append(i)
        return order
    except (ValueError, KeyError, json.JSONDecodeError, TypeError):
        return ids


def select_and_draft(
    conn: sqlite3.Connection,
    config: Config,
    llm: LLMClient | None = None,
    *,
    now: datetime,
    max_posts: int = 1,
    hints: str | None = None,
    voice: str = "",
) -> list[int]:
    """Pick the most post-worthy commit(s) from the recent window and draft them.

    Looks back ``commit_window_days``, ranks meaningful unconsumed events, and drafts
    up to ``max_posts`` (skipping near-duplicates). The un-chosen events simply age out.
    """
    if max_posts <= 0:
        return []
    window_start = (now - timedelta(days=config.commit_window_days)).isoformat()
    rows = conn.execute(
        "SELECT id, summary, topic FROM git_events "
        "WHERE is_meaningful = 1 AND consumed = 0 AND created_at >= ? ORDER BY id DESC",
        (window_start,),
    ).fetchall()
    if not rows:
        return []

    order = _rank_events(llm, config, rows)
    created: list[int] = []
    for event_id in order:
        if len(created) >= max_posts:
            break
        # Recompute recent each iteration so same-run drafts also de-dup against each other.
        recent = dedupe.recent_post_texts(conn)
        draft_id = generate_draft(
            conn, event_id, config, llm, hints=hints, voice=voice, recent=recent
        )
        if draft_id is not None:
            created.append(draft_id)
    return created
