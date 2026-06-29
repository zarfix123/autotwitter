"""Post de-duplication: keep the feed from repeating itself.

One rich commit can legitimately fuel a commit post *and* a tie-in — but they must
say genuinely different things. Two guards work together:

  * ``recent_post_texts`` feeds recent bodies into drafting prompts ("don't reuse
    these angles"), the prompt-level deterrent.
  * ``too_similar`` is the deterministic backstop: a word-overlap (Jaccard) check so
    a near-duplicate draft is caught and skipped/redrafted even if the model ignores
    the prompt.
"""

from __future__ import annotations

import re
import sqlite3

_WORD_RE = re.compile(r"[a-z0-9']+")


def _words(text: str) -> set[str]:
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 2}


def jaccard(a: str, b: str) -> float:
    """Word-set overlap of two texts, 0..1."""
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def too_similar(body: str, recent: list[str], *, threshold: float = 0.6) -> bool:
    """True if ``body`` overlaps any recent post at/above ``threshold`` (a near-dup)."""
    return any(jaccard(body, r) >= threshold for r in recent)


def recent_post_texts(conn: sqlite3.Connection, *, limit: int = 20) -> list[str]:
    """Bodies of recently drafted/scheduled/posted ORIGINAL posts (newest first)."""
    rows = conn.execute(
        "SELECT body FROM drafts WHERE status IN ('draft','scheduled','posted') "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [r["body"] for r in rows if r["body"]]
