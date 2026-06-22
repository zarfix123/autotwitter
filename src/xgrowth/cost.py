"""Cost tracking for the Claude and X APIs.

Records every billable call to api_usage and exposes a weekly-spend query so the
scheduler/monitor can back off as the configured weekly cap is approached.

Prices are constants here (they change rarely); update if Anthropic/X revise them.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from . import audit

# Claude: USD per 1M tokens (input, output). Verified against current catalog.
CLAUDE_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
}

# X API: USD per call (from the brief's 2026 pay-per-use rates).
X_PRICES: dict[str, float] = {
    "post_create": 0.015,
    "post_create_with_url": 0.20,  # avoided by link-in-first-reply
    "post_read": 0.005,
    "user_read": 0.010,
    "owned_read": 0.001,  # own posts / followers
    # X publishes no per-call figure for follows; treat as a write and estimate.
    "follow": 0.010,
}


def record_claude(
    conn: sqlite3.Connection,
    model: str,
    input_tokens: int,
    output_tokens: int,
    *,
    op: str = "message",
    cache_read_tokens: int = 0,
) -> float:
    """Record a Claude call and return its USD cost.

    Cached input is billed at ~0.1x; we approximate that here.
    """
    in_price, out_price = CLAUDE_PRICES.get(model, (3.0, 15.0))
    billable_input = max(input_tokens - cache_read_tokens, 0)
    cost = (
        billable_input * in_price / 1_000_000
        + cache_read_tokens * in_price * 0.1 / 1_000_000
        + output_tokens * out_price / 1_000_000
    )
    _insert(conn, "claude", f"{op}:{model}", input_tokens + output_tokens, cost)
    return cost


def record_x(conn: sqlite3.Connection, op: str, count: int = 1) -> float:
    """Record an X API call and return its USD cost."""
    unit = X_PRICES.get(op, 0.0)
    cost = unit * count
    _insert(conn, "x", op, count, cost)
    return cost


def _insert(conn: sqlite3.Connection, provider: str, op: str, units: float, cost: float) -> None:
    conn.execute(
        "INSERT INTO api_usage(provider, op, units, cost_usd, created_at) VALUES(?,?,?,?,?)",
        (provider, op, units, cost, audit.now_iso()),
    )
    conn.commit()


def spend_since(conn: sqlite3.Connection, since: datetime) -> float:
    row = conn.execute(
        "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM api_usage WHERE created_at >= ?",
        (since.isoformat(),),
    ).fetchone()
    return float(row["total"])


def weekly_spend(conn: sqlite3.Connection) -> float:
    return spend_since(conn, datetime.now(UTC) - timedelta(days=7))


def over_weekly_cap(conn: sqlite3.Connection, cap_usd: float, *, threshold: float = 0.9) -> bool:
    """True once weekly spend reaches ``threshold`` of the cap (default 90%)."""
    if cap_usd <= 0:
        return False
    return weekly_spend(conn) >= cap_usd * threshold
