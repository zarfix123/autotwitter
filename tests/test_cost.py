"""Cost tracker: Claude + X recording, weekly spend, and cap detection."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xgrowth import cost


def test_record_claude_returns_positive_cost(conn):
    c = cost.record_claude(conn, "claude-sonnet-4-6", input_tokens=1000, output_tokens=500)
    assert c > 0
    # 1000 in * $3/1M + 500 out * $15/1M = 0.003 + 0.0075
    assert abs(c - 0.0105) < 1e-9


def test_cache_read_discounts_input(conn):
    full = cost.record_claude(conn, "claude-sonnet-4-6", 1000, 0)
    cached = cost.record_claude(conn, "claude-sonnet-4-6", 1000, 0, cache_read_tokens=1000)
    assert cached < full


def test_record_x_uses_price_table(conn):
    assert cost.record_x(conn, "post_create") == 0.015
    assert cost.record_x(conn, "post_create_with_url") == 0.20


def test_weekly_spend_and_cap(conn):
    cost.record_x(conn, "post_create_with_url")  # 0.20
    assert cost.weekly_spend(conn) >= 0.20
    assert cost.over_weekly_cap(conn, cap_usd=0.20) is True
    assert cost.over_weekly_cap(conn, cap_usd=100.0) is False


def test_old_spend_excluded_from_weekly(conn):
    old = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    conn.execute(
        "INSERT INTO api_usage(provider, op, units, cost_usd, created_at) "
        "VALUES('x','post_create',1,5.0,?)",
        (old,),
    )
    conn.commit()
    assert cost.weekly_spend(conn) == 0.0
