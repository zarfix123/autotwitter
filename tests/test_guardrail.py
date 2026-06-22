"""Static guardrail: auto-engagement must be structurally impossible.

These assertions encode the safety contract from the plan. They scan the source
of src/xgrowth so a future change that smuggles an engagement call outside the
gate, or mints a token outside the Telegram handler, fails CI.
(Expanded further in Phase 4.)
"""

from __future__ import annotations

import pathlib

SRC = pathlib.Path(__file__).resolve().parents[1] / "src" / "xgrowth"


def files_containing(substr: str) -> set[str]:
    return {p.name for p in SRC.glob("*.py") if substr in p.read_text()}


def test_follow_endpoint_only_in_engagement():
    # tweepy's follow endpoint may appear in exactly one module.
    assert files_containing("follow_user") == {"engagement.py"}


def test_mint_token_only_in_engagement_and_telegram():
    # Defined in engagement.py, called only from telegram_bot.py (the human tap).
    assert files_containing("mint_approval_token") == {"engagement.py", "telegram_bot.py"}


def test_gate_only_in_engagement_and_telegram():
    assert files_containing("engagement_gate") == {"engagement.py", "telegram_bot.py"}


def test_engager_methods_called_only_in_engagement():
    # The engager's reply_to/follow are invoked only inside the gate's _perform_*.
    assert files_containing("engager.reply_to(") == {"engagement.py"}
    assert files_containing("engager.follow(") == {"engagement.py"}


def test_original_poster_has_no_engagement_endpoints():
    text = (SRC / "x_client.py").read_text()
    assert "follow_user" not in text
    assert "engagement_gate" not in text
    assert "mint_approval_token" not in text


def test_read_surface_has_no_engagement_endpoints():
    text = (SRC / "x_read.py").read_text()
    assert "follow_user" not in text
    assert "create_tweet" not in text
