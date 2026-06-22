"""Monitor: read-only discovery + ranking of reply opportunities.

Pulls fresh posts from target accounts + keyword searches, ranks them by
relevance (Haiku, one batched call) x freshness x account size, and writes
``reply_opportunities`` and ``follow_candidates``. It imports no engagement code
and performs no writes to X — it only reads and ranks.
"""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime

from . import audit, cost, db
from .config import Config
from .llm import LLMClient
from .x_read import Tweet, XReader


def _age_minutes(created_at: str, now: datetime) -> float | None:
    if not created_at:
        return None
    try:
        dt = datetime.fromisoformat(created_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return max((now - dt).total_seconds() / 60.0, 0.0)


def _freshness_factor(age_min: float | None, max_age: int) -> float:
    if age_min is None:
        return 0.5
    if age_min >= max_age:
        return 0.0
    return 1.0 - (age_min / max_age)


def _size_factor(followers: int | None) -> float:
    return math.log10((followers or 0) + 10)


def rank_relevance(
    llm: LLMClient | None, model: str, clusters: list[str], candidates: list[Tweet]
) -> dict[str, float]:
    """Return {tweet_id: relevance 0..1}. One batched call; 0.5 fallback."""
    if not candidates:
        return {}
    if llm is None:
        return {t.id: 0.5 for t in candidates}
    numbered = "\n".join(f"{i}. {t.text}" for i, t in enumerate(candidates))
    system = (
        "You rate how relevant each post is for a founder to reply to, given their "
        "topic clusters. Relevant = on-topic, invites a substantive reply, worth being "
        "an early reply on. Respond ONLY with a compact JSON array of "
        '{"i": int, "relevance": number 0..1}, one per post.'
    )
    user = f"Topic clusters: {clusters}\n\nPosts:\n{numbered}"
    text = llm.complete(model=model, system=system, user=user, max_tokens=600)
    try:
        data = json.loads(text[text.index("[") : text.rindex("]") + 1])
        scores: dict[str, float] = {}
        for entry in data:
            idx = int(entry["i"])
            if 0 <= idx < len(candidates):
                scores[candidates[idx].id] = max(0.0, min(1.0, float(entry["relevance"])))
        # Anything the model skipped gets a neutral default.
        for t in candidates:
            scores.setdefault(t.id, 0.5)
        return scores
    except (ValueError, KeyError, json.JSONDecodeError, TypeError):
        return {t.id: 0.5 for t in candidates}


def _gather(config: Config, reader: XReader) -> list[Tweet]:
    seen: dict[str, Tweet] = {}
    for handle in config.target_accounts:
        for t in reader.user_recent(handle, max_results=5):
            seen.setdefault(t.id, t)
    for kw in config.keywords:
        # Exclude retweets/replies; recent English originals are best reply targets.
        query = f"{kw} -is:retweet -is:reply lang:en"
        for t in reader.search_recent(query, max_results=10):
            seen.setdefault(t.id, t)
    return list(seen.values())


def scan(
    conn: sqlite3.Connection,
    config: Config,
    reader: XReader,
    llm: LLMClient | None = None,
    *,
    now: datetime | None = None,
) -> list[int]:
    """Run one monitor scan. Returns created reply_opportunity ids. No engagement."""
    now = now or datetime.now(UTC)

    if db.is_paused(conn):
        audit.log(conn, "monitor.skipped", detail={"reason": "paused"})
        return []
    if cost.over_weekly_cap(conn, config.weekly_cost_cap_usd):
        audit.log(conn, "monitor.skipped", detail={"reason": "weekly_cost_cap"})
        return []

    candidates = _gather(config, reader)

    # Freshness filter — favor posts within the configured window.
    fresh: list[tuple[Tweet, float | None]] = []
    for t in candidates:
        age = _age_minutes(t.created_at, now)
        if age is not None and age > config.opportunity_max_age_minutes:
            continue
        fresh.append((t, age))
    if not fresh:
        return []

    # Skip anything we already have an opportunity for.
    existing = {
        r["target_tweet_id"]
        for r in conn.execute("SELECT target_tweet_id FROM reply_opportunities").fetchall()
    }
    fresh = [(t, age) for t, age in fresh if t.id not in existing]
    if not fresh:
        return []

    # Backfill missing follower counts in one batched read.
    missing = sorted({t.author_handle for t, _ in fresh if t.author_followers is None and t.author_handle})
    if missing:
        counts = reader.follower_counts(missing)
        for t, _ in fresh:
            if t.author_followers is None:
                t.author_followers = counts.get(t.author_handle)

    fresh_tweets = [t for t, _ in fresh]
    relevance = rank_relevance(llm, config.models.classify, config.topic_clusters, fresh_tweets)

    scored = []
    for t, age in fresh:
        rel = relevance.get(t.id, 0.5)
        score = rel * _freshness_factor(age, config.opportunity_max_age_minutes) * _size_factor(t.author_followers)
        scored.append((score, rel, age, t))
    scored.sort(key=lambda x: x[0], reverse=True)

    cap = max(config.daily_reply_queue_size * 3, 15)
    created: list[int] = []
    for rank, (_score, rel, age, t) in enumerate(scored[:cap]):
        cur = conn.execute(
            "INSERT INTO reply_opportunities(target_tweet_id, author_handle, "
            "author_followers, text, posted_at, freshness_min, relevance_score, rank, "
            "status, created_at) VALUES(?,?,?,?,?,?,?,?,'queued',?)",
            (
                t.id, t.author_handle, t.author_followers, t.text, t.created_at,
                age, rel, rank, now.isoformat(),
            ),
        )
        created.append(int(cur.lastrowid))
    conn.commit()

    _derive_follow_candidates(conn, config, scored, now)

    audit.log(conn, "monitor.scanned", detail={"candidates": len(candidates), "queued": len(created)})
    return created


def _derive_follow_candidates(
    conn: sqlite3.Connection, config: Config, scored: list, now: datetime
) -> None:
    """Suggest a few follow candidates from high-relevance authors (if enabled)."""
    if config.max_follows_per_day <= 0:
        return
    existing = {r["handle"] for r in conn.execute("SELECT handle FROM follow_candidates").fetchall()}
    added = 0
    for _score, rel, _age, t in scored:
        if added >= config.max_follows_per_day:
            break
        if rel < 0.6 or not t.author_handle or t.author_handle in existing:
            continue
        conn.execute(
            "INSERT INTO follow_candidates(handle, reason, score, status, created_at) "
            "VALUES(?,?,?,'queued',?)",
            (t.author_handle, f"high-relevance author (rel={rel:.2f})", rel, now.isoformat()),
        )
        existing.add(t.author_handle)
        added += 1
    conn.commit()
