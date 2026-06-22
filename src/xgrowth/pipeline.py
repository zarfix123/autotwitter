"""Phase 1 orchestration: git -> content -> schedule -> post, plus control ops.

Each step is independently testable; this just wires them in order. The posting
cycle is what APScheduler invokes on a cron cadence.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime

from . import audit, content_gen, db, git_watcher
from . import poster as poster_mod
from .config import Config
from .git_watcher import Classifier
from .github_client import CommitSource
from .llm import LLMClient
from .scheduler import schedule_pending
from .x_client import XPoster


@dataclass
class CycleResult:
    new_events: list[int]
    new_drafts: list[int]
    scheduled: list[tuple[int, datetime]]
    published: list[int]


def watch_and_draft(
    conn: sqlite3.Connection,
    config: Config,
    source: CommitSource,
    classifier: Classifier,
    llm: LLMClient | None,
    *,
    now: datetime,
) -> tuple[list[int], list[int], list[tuple[int, datetime]]]:
    """Poll repos, draft meaningful events, and schedule the drafts."""
    new_events = git_watcher.run(conn, config, source, classifier)
    new_drafts = content_gen.generate_pending(conn, config, llm)
    scheduled = schedule_pending(conn, config, now=now)
    return new_events, new_drafts, scheduled


def run_cycle(
    conn: sqlite3.Connection,
    config: Config,
    source: CommitSource,
    classifier: Classifier,
    llm: LLMClient | None,
    poster: XPoster,
    *,
    now: datetime,
) -> CycleResult:
    """Full Phase 1 cycle. Safe to call repeatedly; honors the paused flag."""
    new_events, new_drafts, scheduled = watch_and_draft(
        conn, config, source, classifier, llm, now=now
    )
    published = poster_mod.publish_due(conn, config, poster, now=now)
    return CycleResult(new_events, new_drafts, scheduled, published)


# ---- control ops (kill switch) ---------------------------------------------
def pause_and_clear(conn: sqlite3.Connection) -> int:
    """Kill switch: pause posting and clear the send queue. Returns rows cleared."""
    db.set_paused(conn, True)
    cur = conn.execute(
        "UPDATE drafts SET status = 'killed' WHERE status IN ('scheduled', 'draft')"
    )
    conn.commit()
    cleared = cur.rowcount
    audit.log(conn, "kill_switch.activated", detail={"cleared": cleared})
    return cleared


def resume(conn: sqlite3.Connection) -> None:
    db.set_paused(conn, False)
    audit.log(conn, "kill_switch.resumed")


def status(conn: sqlite3.Connection) -> dict:
    def count(sql: str, *args) -> int:
        return int(conn.execute(sql, args).fetchone()[0])

    return {
        "paused": db.is_paused(conn),
        "git_events": count("SELECT COUNT(*) FROM git_events"),
        "drafts_pending": count("SELECT COUNT(*) FROM drafts WHERE status = 'draft'"),
        "drafts_scheduled": count("SELECT COUNT(*) FROM drafts WHERE status = 'scheduled'"),
        "drafts_posted": count("SELECT COUNT(*) FROM drafts WHERE status = 'posted'"),
        "weekly_spend_usd": round(
            float(
                conn.execute(
                    "SELECT COALESCE(SUM(cost_usd),0) FROM api_usage "
                    "WHERE created_at >= datetime('now','-7 days')"
                ).fetchone()[0]
            ),
            4,
        ),
    }
