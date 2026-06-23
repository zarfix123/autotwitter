"""SQLite state: the single source of truth for dedup, queue, audit, and cost.

A single-writer process (one always-on instance) makes SQLite a good fit. We use
WAL mode and a small connection helper. Schema is created idempotently on init.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# Phase 1 tables are fully wired. Phase 2/3 tables (reply_opportunities,
# reply_drafts, follow_candidates, approvals, analytics) are created here so the
# schema is stable, but no Phase 1 code writes other-account engagement rows.
SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS repos (
    full_name      TEXT PRIMARY KEY,
    last_polled_at TEXT,
    last_seen_sha  TEXT
);

CREATE TABLE IF NOT EXISTS git_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    repo          TEXT NOT NULL,
    commit_shas   TEXT NOT NULL,          -- json array
    summary       TEXT,                   -- scrubbed, human-readable change summary
    dedup_key     TEXT NOT NULL UNIQUE,   -- hash(repo + sorted shas)
    is_meaningful INTEGER NOT NULL DEFAULT 0,
    topic         TEXT,
    consumed      INTEGER NOT NULL DEFAULT 0,  -- 1 once a draft has been generated
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS drafts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    git_event_id     INTEGER,
    kind             TEXT NOT NULL DEFAULT 'post',   -- post | thread
    body             TEXT NOT NULL,
    first_reply_link TEXT,
    status           TEXT NOT NULL DEFAULT 'draft',  -- draft|scheduled|posted|failed|killed
    scheduled_at     TEXT,
    posted_tweet_id  TEXT,
    reply_tweet_id   TEXT,
    model            TEXT,
    topic            TEXT,                           -- own topic for source-less (AI-news) drafts
    created_at       TEXT NOT NULL,
    FOREIGN KEY (git_event_id) REFERENCES git_events(id)
);

CREATE TABLE IF NOT EXISTS posted_history (
    tweet_id  TEXT PRIMARY KEY,
    kind      TEXT NOT NULL,        -- original | self_reply_link
    draft_id  INTEGER,
    posted_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_usage (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    provider   TEXT NOT NULL,       -- claude | x | github
    op         TEXT NOT NULL,
    units      REAL NOT NULL DEFAULT 0,   -- tokens or call count
    cost_usd   REAL NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event       TEXT NOT NULL,
    entity_type TEXT,
    entity_id   TEXT,
    detail      TEXT,               -- json
    created_at  TEXT NOT NULL
);

-- Phase 2/3 (created now for schema stability; not written by Phase 1) ----------
CREATE TABLE IF NOT EXISTS reply_opportunities (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    target_tweet_id  TEXT NOT NULL,
    author_handle    TEXT,
    author_followers INTEGER,
    text             TEXT,
    posted_at        TEXT,
    freshness_min    REAL,
    relevance_score  REAL,
    rank             INTEGER,
    status           TEXT NOT NULL DEFAULT 'queued',
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reply_drafts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    opportunity_id INTEGER,
    text           TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'draft',
    sent_tweet_id  TEXT,
    created_at     TEXT NOT NULL,
    FOREIGN KEY (opportunity_id) REFERENCES reply_opportunities(id)
);

CREATE TABLE IF NOT EXISTS follow_candidates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    handle     TEXT NOT NULL,
    reason     TEXT,
    score      REAL,
    status     TEXT NOT NULL DEFAULT 'queued',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
    token            TEXT PRIMARY KEY,
    item_type        TEXT NOT NULL,     -- reply | follow
    item_id          TEXT NOT NULL,
    telegram_user_id INTEGER NOT NULL,
    created_at       TEXT NOT NULL,
    expires_at       TEXT NOT NULL,
    used_at          TEXT
);

CREATE TABLE IF NOT EXISTS analytics (
    tweet_id    TEXT NOT NULL,
    impressions INTEGER,
    likes       INTEGER,
    reposts     INTEGER,
    replies     INTEGER,
    fetched_at  TEXT NOT NULL
);

-- AI-news content source: trending stories discovered for opinion/tie-in posts.
CREATE TABLE IF NOT EXISTS news_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL UNIQUE,   -- source id (e.g. HN objectID); dedup key
    source          TEXT NOT NULL DEFAULT 'hn',
    title           TEXT NOT NULL,
    url             TEXT NOT NULL,          -- article URL (-> first_reply_link)
    points          INTEGER NOT NULL DEFAULT 0,
    num_comments    INTEGER NOT NULL DEFAULT 0,
    topic           TEXT,
    is_meaningful   INTEGER NOT NULL DEFAULT 0,
    consumed        INTEGER NOT NULL DEFAULT 0,  -- 1 once a draft has been generated
    item_created_at TEXT,                   -- when the story was posted at the source
    created_at      TEXT NOT NULL           -- when we ingested it
);
"""


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection with sane defaults. Use ``:memory:`` for tests."""
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if db_path != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables idempotently, then run lightweight column migrations."""
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns to existing tables that predate them (SQLite has no ADD COLUMN IF NOT EXISTS)."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(drafts)").fetchall()}
    if "topic" not in cols:
        conn.execute("ALTER TABLE drafts ADD COLUMN topic TEXT")


# ---- settings (paused flag / kill switch state, cursors) --------------------
def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def is_paused(conn: sqlite3.Connection) -> bool:
    return get_setting(conn, "paused", "0") == "1"


def set_paused(conn: sqlite3.Connection, paused: bool) -> None:
    set_setting(conn, "paused", "1" if paused else "0")
