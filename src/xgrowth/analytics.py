"""Analytics + feedback loop.

`pull` snapshots our own posts' metrics (cheap owned reads) into the `analytics`
time series. `insights` turns the latest snapshots into lightweight signals —
which topics and which posting hours land best — that feed back into the content
generator's prompt and the scheduler's window choice.

Deterministic on purpose (no LLM): the feedback is a gentle nudge, and we only
emit hints once there's enough data to avoid over-fitting on noise.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from . import audit, cost, db
from .config import Config
from .x_read import XReader


@dataclass
class Insights:
    top_topics: list[str] = field(default_factory=list)
    best_hours: list[int] = field(default_factory=list)
    hint_text: str = ""


@dataclass
class ReplyInsights:
    """Learned reply-performance signals. Factors are multipliers around 1.0 that
    re-rank reply opportunities (1.0 = neutral / no data)."""

    author_factors: dict[str, float] = field(default_factory=dict)
    topic_factors: dict[str, float] = field(default_factory=dict)
    top_authors: list[str] = field(default_factory=list)
    top_topics: list[str] = field(default_factory=list)
    hint_text: str = ""

    def author_factor(self, handle: str | None) -> float:
        return self.author_factors.get(handle or "", 1.0)

    def topic_factor(self, topic: str | None) -> float:
        return self.topic_factors.get(topic or "", 1.0)


def _engagement_score(impressions: int, likes: int, reposts: int, replies: int) -> float:
    # Weight active engagement above passive impressions; impressions break ties.
    return likes + 2 * reposts + 3 * replies + impressions / 1000.0


def pull(
    conn: sqlite3.Connection,
    config: Config,
    reader: XReader,
    *,
    now: datetime | None = None,
    lookback_days: int = 14,
) -> list[str]:
    """Snapshot metrics for our recent original tweets. Returns snapshotted ids."""
    now = now or datetime.now(UTC)
    if db.is_paused(conn):
        audit.log(conn, "analytics.skipped", detail={"reason": "paused"})
        return []
    if cost.over_weekly_cap(conn, config.weekly_cost_cap_usd):
        audit.log(conn, "analytics.skipped", detail={"reason": "weekly_cost_cap"})
        return []

    since = (now - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT tweet_id FROM posted_history WHERE kind = 'original' AND posted_at >= ?",
        (since,),
    ).fetchall()
    ids = [r["tweet_id"] for r in rows]
    if not ids:
        return []

    metrics = reader.tweet_metrics(ids)
    snapshotted: list[str] = []
    for tid, m in metrics.items():
        conn.execute(
            "INSERT INTO analytics(tweet_id, impressions, likes, reposts, replies, "
            "fetched_at) VALUES(?,?,?,?,?,?)",
            (tid, m.impressions, m.likes, m.reposts, m.replies, now.isoformat()),
        )
        snapshotted.append(tid)
    conn.commit()
    audit.log(conn, "analytics.pulled", detail={"count": len(snapshotted)})
    return snapshotted


def pull_replies(
    conn: sqlite3.Connection,
    config: Config,
    reader: XReader,
    *,
    now: datetime | None = None,
    lookback_days: int = 14,
) -> list[str]:
    """Snapshot metrics for our recently sent replies (owned reads). Returns ids."""
    now = now or datetime.now(UTC)
    if db.is_paused(conn):
        audit.log(conn, "reply_analytics.skipped", detail={"reason": "paused"})
        return []
    if cost.over_weekly_cap(conn, config.weekly_cost_cap_usd):
        audit.log(conn, "reply_analytics.skipped", detail={"reason": "weekly_cost_cap"})
        return []

    since = (now - timedelta(days=lookback_days)).isoformat()
    rows = conn.execute(
        "SELECT sent_tweet_id FROM reply_drafts "
        "WHERE status = 'sent' AND sent_tweet_id IS NOT NULL AND created_at >= ?",
        (since,),
    ).fetchall()
    ids = [r["sent_tweet_id"] for r in rows]
    if not ids:
        return []

    metrics = reader.tweet_metrics(ids)
    snapped: list[str] = []
    for tid, m in metrics.items():
        conn.execute(
            "INSERT INTO reply_analytics(reply_tweet_id, impressions, likes, reposts, "
            "replies, fetched_at) VALUES(?,?,?,?,?,?)",
            (tid, m.impressions, m.likes, m.reposts, m.replies, now.isoformat()),
        )
        snapped.append(tid)
    conn.commit()
    audit.log(conn, "reply_analytics.pulled", detail={"count": len(snapped)})
    return snapped


def _latest_reply_snapshots(conn: sqlite3.Connection) -> dict[str, float]:
    """reply_tweet_id -> engagement score, most recent snapshot per reply."""
    rows = conn.execute(
        "SELECT a.reply_tweet_id, a.impressions, a.likes, a.reposts, a.replies "
        "FROM reply_analytics a JOIN ("
        "  SELECT reply_tweet_id, MAX(fetched_at) AS mx FROM reply_analytics "
        "  GROUP BY reply_tweet_id"
        ") m ON a.reply_tweet_id = m.reply_tweet_id AND a.fetched_at = m.mx"
    ).fetchall()
    return {
        r["reply_tweet_id"]: _engagement_score(
            r["impressions"], r["likes"], r["reposts"], r["replies"]
        )
        for r in rows
    }


def _reply_author_topic(conn: sqlite3.Connection) -> dict[str, tuple[str | None, str | None]]:
    """reply_tweet_id -> (author we replied to, topic) for attribution."""
    rows = conn.execute(
        "SELECT rd.sent_tweet_id AS tid, ro.author_handle AS author, ro.topic AS topic "
        "FROM reply_drafts rd JOIN reply_opportunities ro ON ro.id = rd.opportunity_id "
        "WHERE rd.sent_tweet_id IS NOT NULL"
    ).fetchall()
    return {r["tid"]: (r["author"], r["topic"]) for r in rows}


def reply_insights(
    conn: sqlite3.Connection, *, min_samples: int = 3, min_key_samples: int = 2
) -> ReplyInsights:
    """Per-author/topic reply-performance factors. Empty (neutral) below min_samples."""
    scores = _latest_reply_snapshots(conn)
    if len(scores) < min_samples:
        return ReplyInsights()

    meta = _reply_author_topic(conn)
    author_vals: dict[str, list[float]] = {}
    topic_vals: dict[str, list[float]] = {}
    for tid, score in scores.items():
        author, topic = meta.get(tid, (None, None))
        if author:
            author_vals.setdefault(author, []).append(score)
        if topic:
            topic_vals.setdefault(topic, []).append(score)

    global_avg = sum(scores.values()) / len(scores)

    def _factors(vals: dict[str, list[float]]) -> dict[str, float]:
        out: dict[str, float] = {}
        for key, samples in vals.items():
            if len(samples) < min_key_samples:
                continue  # don't overfit to one lucky (or unlucky) reply
            avg = sum(samples) / len(samples)
            factor = avg / global_avg if global_avg > 0 else 1.0
            out[key] = max(0.5, min(2.0, factor))  # clamp so nothing dominates
        return out

    def _top(vals: dict[str, list[float]], n: int = 3) -> list[str]:
        avgs = {
            k: sum(v) / len(v) for k, v in vals.items() if len(v) >= min_key_samples
        }
        return [k for k, _ in sorted(avgs.items(), key=lambda kv: kv[1], reverse=True)[:n]]

    author_factors = _factors(author_vals)
    topic_factors = _factors(topic_vals)
    top_authors = _top(author_vals)
    top_topics = _top(topic_vals)

    parts: list[str] = []
    if top_authors:
        parts.append("replies to " + ", ".join("@" + a for a in top_authors) + " land best")
    if top_topics:
        parts.append("topics that land: " + ", ".join(top_topics))
    hint_text = ("Reply performance: " + "; ".join(parts) + ".") if parts else ""

    return ReplyInsights(
        author_factors=author_factors,
        topic_factors=topic_factors,
        top_authors=top_authors,
        top_topics=top_topics,
        hint_text=hint_text,
    )


def _latest_snapshots(conn: sqlite3.Connection) -> dict[str, float]:
    """tweet_id -> engagement score, using the most recent snapshot per tweet."""
    rows = conn.execute(
        "SELECT a.tweet_id, a.impressions, a.likes, a.reposts, a.replies "
        "FROM analytics a JOIN ("
        "  SELECT tweet_id, MAX(fetched_at) AS mx FROM analytics GROUP BY tweet_id"
        ") m ON a.tweet_id = m.tweet_id AND a.fetched_at = m.mx"
    ).fetchall()
    return {
        r["tweet_id"]: _engagement_score(
            r["impressions"], r["likes"], r["reposts"], r["replies"]
        )
        for r in rows
    }


def _topic_and_hour(conn: sqlite3.Connection) -> dict[str, tuple[str | None, int | None]]:
    """tweet_id -> (topic, posted hour) for original posts."""
    rows = conn.execute(
        "SELECT ph.tweet_id, COALESCE(d.topic, ge.topic) AS topic, ph.posted_at AS posted_at "
        "FROM posted_history ph "
        "LEFT JOIN drafts d ON d.id = ph.draft_id "
        "LEFT JOIN git_events ge ON ge.id = d.git_event_id "
        "WHERE ph.kind = 'original'"
    ).fetchall()
    out: dict[str, tuple[str | None, int | None]] = {}
    for r in rows:
        hour: int | None = None
        if r["posted_at"]:
            try:
                hour = datetime.fromisoformat(r["posted_at"]).hour
            except ValueError:
                hour = None
        out[r["tweet_id"]] = (r["topic"], hour)
    return out


def _avg_by(pairs: list[tuple[str | int, float]], top_n: int) -> list[str | int]:
    sums: dict[str | int, float] = {}
    counts: dict[str | int, int] = {}
    for key, score in pairs:
        sums[key] = sums.get(key, 0.0) + score
        counts[key] = counts.get(key, 0) + 1
    avgs = {k: sums[k] / counts[k] for k in sums}
    return [k for k, _ in sorted(avgs.items(), key=lambda kv: kv[1], reverse=True)[:top_n]]


def insights(conn: sqlite3.Connection, *, min_posts: int = 5) -> Insights:
    """Compute top topics + best posting hours from snapshots. Empty below min_posts."""
    scores = _latest_snapshots(conn)
    if len(scores) < min_posts:
        return Insights()

    meta = _topic_and_hour(conn)
    topic_pairs: list[tuple[str, float]] = []
    hour_pairs: list[tuple[int, float]] = []
    for tid, score in scores.items():
        topic, hour = meta.get(tid, (None, None))
        if topic:
            topic_pairs.append((topic, score))
        if hour is not None:
            hour_pairs.append((hour, score))

    top_topics = [str(t) for t in _avg_by(topic_pairs, 3)]  # type: ignore[arg-type]
    best_hours = [int(h) for h in _avg_by(hour_pairs, 3)]  # type: ignore[arg-type]

    parts: list[str] = []
    if top_topics:
        parts.append("top topics — " + ", ".join(top_topics))
    if best_hours:
        parts.append("best posting hours — " + ", ".join(f"{h:02d}:00" for h in best_hours))
    hint_text = ("Recent performance: " + "; ".join(parts) + ".") if parts else ""

    return Insights(top_topics=top_topics, best_hours=best_hours, hint_text=hint_text)
