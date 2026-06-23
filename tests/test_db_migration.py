"""DB migrations: drafts.topic is added idempotently to pre-existing databases."""

from __future__ import annotations

from xgrowth import db


def _draft_columns(conn) -> set[str]:
    return {r["name"] for r in conn.execute("PRAGMA table_info(drafts)").fetchall()}


def test_topic_added_to_legacy_drafts_table():
    conn = db.connect(":memory:")
    # Simulate a database created before the `topic` column existed.
    conn.execute(
        "CREATE TABLE drafts ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT, git_event_id INTEGER, kind TEXT, body TEXT,"
        " first_reply_link TEXT, status TEXT, scheduled_at TEXT, posted_tweet_id TEXT,"
        " reply_tweet_id TEXT, model TEXT, created_at TEXT)"
    )
    conn.commit()
    assert "topic" not in _draft_columns(conn)

    db.init_db(conn)  # should add the missing column without error
    assert "topic" in _draft_columns(conn)

    db.init_db(conn)  # idempotent: a second run is a no-op
    assert "topic" in _draft_columns(conn)
    conn.close()


def test_fresh_db_has_topic_column():
    conn = db.connect(":memory:")
    db.init_db(conn)
    assert "topic" in _draft_columns(conn)
    conn.close()
