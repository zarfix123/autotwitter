"""Append-only audit log. Every meaningful action records a row here."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def log(
    conn: sqlite3.Connection,
    event: str,
    *,
    entity_type: str | None = None,
    entity_id: str | int | None = None,
    detail: dict | None = None,
) -> None:
    conn.execute(
        "INSERT INTO audit_log(event, entity_type, entity_id, detail, created_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (
            event,
            entity_type,
            str(entity_id) if entity_id is not None else None,
            json.dumps(detail) if detail is not None else None,
            now_iso(),
        ),
    )
    conn.commit()
