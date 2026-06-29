"""Reply drafter: turn ranked opportunities into sharp, specific drafted replies.

Sonnet drafts in the founder's voice. Recent sent replies are passed in so the
model varies phrasing and never reuses an opener. Replies are forced URL-free and
length-clamped. Drafting only — sending happens later, via the engagement gate,
after a human approves in Telegram.
"""

from __future__ import annotations

import json
import sqlite3

from . import audit
from .config import Config
from .llm import LLMClient
from .textfmt import clamp, strip_urls


def _recent_sent_replies(conn: sqlite3.Connection, limit: int = 10) -> list[str]:
    rows = conn.execute(
        "SELECT text FROM reply_drafts WHERE status = 'sent' ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["text"] for r in rows]


def _system_prompt(
    config: Config, recent: list[str], hints: str | None = None, voice: str = ""
) -> str:
    clusters = ", ".join(config.topic_clusters)
    if voice:
        voice_block = voice
    else:
        samples = "\n".join(f"- {s}" for s in config.voice_samples)
        voice_block = f"Voice samples (match tone — direct, specific, no hype):\n{samples}"
    avoid = "\n".join(f"- {r}" for r in recent) if recent else "(none yet)"
    perf = f"What's been landing (lean into it, don't force): {hints}\n" if hints else ""
    return (
        "You write replies to other people's posts on X, as a solo founder.\n"
        f"Topics you know: {clusters}.\n"
        f"{voice_block}\n"
        f"{perf}\n"
        "Rules for the reply:\n"
        "- Be specific and substantive: add a concrete point, question, or experience.\n"
        "- Never generic ('great post!', 'so true', 'love this').\n"
        "- No links/URLs, no hashtag spam, keep it to one or two sentences.\n"
        "- Vary phrasing; do NOT reuse openers or structure from these recent replies:\n"
        f"{avoid}\n\n"
        'Respond ONLY with compact JSON: {"reply": str}.'
    )


def _fallback_reply(opportunity_text: str) -> str:
    snippet = opportunity_text.strip().split("\n", 1)[0][:80]
    return f"curious how you're approaching this — {snippet}".strip()


def draft_one(
    conn: sqlite3.Connection,
    opportunity_id: int,
    config: Config,
    llm: LLMClient | None,
    *,
    hints: str | None = None,
    voice: str = "",
) -> int:
    """Draft a reply for one opportunity. Returns the reply_draft id."""
    opp = conn.execute(
        "SELECT id, author_handle, text, status FROM reply_opportunities WHERE id = ?",
        (opportunity_id,),
    ).fetchone()
    if opp is None:
        raise ValueError(f"opportunity {opportunity_id} not found")

    if llm is not None:
        recent = _recent_sent_replies(conn)
        text = llm.complete(
            model=config.models.draft,
            system=_system_prompt(config, recent, hints, voice),
            user=f"Post by @{opp['author_handle']}:\n{opp['text']}",
            max_tokens=300,
        )
        try:
            data = json.loads(text[text.index("{") : text.rindex("}") + 1])
            reply = str(data.get("reply", "")).strip()
        except (ValueError, json.JSONDecodeError):
            reply = strip_urls(text).strip()
    else:
        reply = _fallback_reply(opp["text"])

    reply = clamp(strip_urls(reply), config.x_premium)

    cur = conn.execute(
        "INSERT INTO reply_drafts(opportunity_id, text, status, created_at) "
        "VALUES(?,?,'draft',?)",
        (opportunity_id, reply, audit.now_iso()),
    )
    conn.execute(
        "UPDATE reply_opportunities SET status = 'drafted' WHERE id = ?", (opportunity_id,)
    )
    conn.commit()
    draft_id = int(cur.lastrowid)
    audit.log(
        conn, "reply_draft.created", entity_type="reply_draft", entity_id=draft_id,
        detail={"opportunity_id": opportunity_id, "len": len(reply)},
    )
    return draft_id


def draft_pending(
    conn: sqlite3.Connection,
    config: Config,
    llm: LLMClient | None = None,
    *,
    hints: str | None = None,
    voice: str = "",
) -> list[int]:
    """Draft replies for the top queued opportunities, up to the daily queue size."""
    rows = conn.execute(
        "SELECT id FROM reply_opportunities WHERE status = 'queued' "
        "ORDER BY rank ASC, relevance_score DESC LIMIT ?",
        (config.daily_reply_queue_size,),
    ).fetchall()
    return [draft_one(conn, r["id"], config, llm, hints=hints, voice=voice) for r in rows]
