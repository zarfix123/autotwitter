"""News watcher: turn trending AI stories into deduplicated, meaningful news items.

Mirrors ``git_watcher`` for a different source: fetch stories, drop stale/low-signal
ones, dedup by source id, ask the cheap classifier whether each is worth an opinion
or tie-in post, and persist ``news_items`` rows. Read-only and engagement-free —
it never touches X or the engagement gate.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta

from . import audit, cost, db
from .config import Config
from .git_watcher import Classifier, ClassifyResult
from .llm import LLMClient
from .news_source import NewsSource

# Headlines worth a post for an AI/build-in-public founder usually mention one of these.
_AI_KEYWORDS = (
    "ai", "a.i", "llm", "gpt", "agent", "model", "ml", "machine learning", "neural",
    "openai", "anthropic", "claude", "gemini", "llama", "mistral", "diffusion",
    "transformer", "rag", "fine-tun", "inference", "embedding", "chatbot",
)


def news_heuristic_classifier(title: str, topic_clusters: list[str]) -> ClassifyResult:
    """Offline fallback: meaningful if the headline looks AI-relevant."""
    low = title.lower()
    meaningful = any(k in low for k in _AI_KEYWORDS)
    topic = topic_clusters[0] if topic_clusters else "general"
    return ClassifyResult(meaningful=meaningful, topic=topic, summary=title)


def make_news_classifier(llm: LLMClient, model: str) -> Classifier:
    """Classifier backed by the cheap (Haiku) model. No web search at this stage."""

    def classify(title: str, topic_clusters: list[str]) -> ClassifyResult:
        system = (
            "You judge whether a trending tech headline is worth an original post for a "
            "founder building in public in AI — either a sharp opinion on it, or relating "
            "their own work to it. Meaningful = a current AI development, launch, debate, "
            "or trend with a clear angle. Not meaningful = off-topic, pure marketing, "
            "low-substance, or stale. Respond ONLY with compact JSON: "
            '{"meaningful": bool, "topic": str}. topic must be one of the user\'s clusters '
            "or 'general'."
        )
        user = f"Topic clusters: {topic_clusters}\n\nHeadline:\n{title}"
        text = llm.complete(model=model, system=system, user=user, max_tokens=200)
        try:
            data = json.loads(text[text.index("{") : text.rindex("}") + 1])
            return ClassifyResult(
                meaningful=bool(data.get("meaningful", False)),
                topic=str(data.get("topic", "general")),
                summary=title,
            )
        except (ValueError, json.JSONDecodeError):
            return news_heuristic_classifier(title, topic_clusters)

    return classify


def _age_hours(created_at: str | None, now: datetime) -> float | None:
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max((now - dt).total_seconds() / 3600.0, 0.0)


def scan(
    conn: sqlite3.Connection,
    config: Config,
    source: NewsSource,
    classifier: Classifier,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Discover trending AI stories and persist new, meaningful ones. Returns new ids."""
    now = now or datetime.now(UTC)

    if not config.ai_news_enabled:
        return []
    if db.is_paused(conn):
        audit.log(conn, "news.skipped", detail={"reason": "paused"})
        return []
    if cost.over_weekly_cap(conn, config.weekly_cost_cap_usd):
        audit.log(conn, "news.skipped", detail={"reason": "weekly_cost_cap"})
        return []

    since = now - timedelta(hours=config.news_item_max_age_hours)
    items = source.fetch(config.topic_clusters, since_iso=since.isoformat())

    existing = {r["item_id"] for r in conn.execute("SELECT item_id FROM news_items").fetchall()}
    created: list[int] = []
    for item in items:
        if item.item_id in existing:
            continue
        if item.points < config.news_min_points:
            continue
        age = _age_hours(item.item_created_at, now)
        if age is not None and age > config.news_item_max_age_hours:
            continue
        result = classifier(item.title, config.topic_clusters)
        cur = conn.execute(
            "INSERT INTO news_items(item_id, source, title, url, points, num_comments, "
            "topic, is_meaningful, consumed, item_created_at, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,0,?,?)",
            (
                item.item_id,
                item.source,
                item.title,
                item.url,
                item.points,
                item.num_comments,
                result.topic,
                1 if result.meaningful else 0,
                item.item_created_at,
                now.isoformat(),
            ),
        )
        existing.add(item.item_id)
        created.append(int(cur.lastrowid))
    conn.commit()
    audit.log(conn, "news.scanned", detail={"fetched": len(items), "new": len(created)})
    return created
