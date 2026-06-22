"""Telegram approval bot — the only human touchpoint and the only token source.

The decision logic lives in plain, PTB-free functions (testable without the
library, and the *only* place ``mint_approval_token`` is called). The PTB wiring
(``build_application``, ``push_batch``) imports ``telegram`` lazily so the rest of
the suite runs without the dependency installed.

Approve  -> mint a fresh single-use token -> engagement_gate -> X (one action).
Skip     -> mark the item skipped; no token, no X call.
Non-allow-listed user -> denied; no token minted.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from typing import Any

from . import audit, pipeline
from .config import Config
from .engagement import XEngager, engagement_gate, mint_approval_token

ConnFactory = Callable[[], sqlite3.Connection]


# --- callback-data helpers ----------------------------------------------------
def make_callback(decision: str, item_type: str, item_id: int) -> str:
    return f"{decision}:{item_type}:{item_id}"


def parse_callback(data: str) -> tuple[str, str, int]:
    decision, item_type, raw_id = data.split(":")
    return decision, item_type, int(raw_id)


# --- pending queue (for /queue and the daily push) ----------------------------
def pending_items(conn: sqlite3.Connection, config: Config) -> dict[str, list[dict]]:
    replies = [
        dict(r)
        for r in conn.execute(
            "SELECT rd.id, rd.text, ro.author_handle, ro.text AS post_text "
            "FROM reply_drafts rd JOIN reply_opportunities ro ON ro.id = rd.opportunity_id "
            "WHERE rd.status = 'draft' ORDER BY rd.id LIMIT ?",
            (config.daily_reply_queue_size,),
        ).fetchall()
    ]
    follows = []
    if config.max_follows_per_day > 0:
        follows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, handle, reason FROM follow_candidates WHERE status = 'queued' "
                "ORDER BY score DESC LIMIT ?",
                (config.max_follows_per_day,),
            ).fetchall()
        ]
    return {"replies": replies, "follows": follows}


# --- the testable decision core (ONLY caller of mint_approval_token) ----------
def _mark_skip(conn: sqlite3.Connection, item_type: str, item_id: int) -> None:
    if item_type == "reply":
        conn.execute("UPDATE reply_drafts SET status = 'skipped' WHERE id = ?", (item_id,))
        conn.execute(
            "UPDATE reply_opportunities SET status = 'skipped' WHERE id = "
            "(SELECT opportunity_id FROM reply_drafts WHERE id = ?)",
            (item_id,),
        )
    elif item_type == "follow":
        conn.execute("UPDATE follow_candidates SET status = 'skipped' WHERE id = ?", (item_id,))
    conn.commit()
    audit.log(conn, "approval.skipped", entity_type=item_type, entity_id=item_id)


def handle_decision(
    conn: sqlite3.Connection,
    config: Config,
    allowed_user_id: int | None,
    engager: XEngager,
    telegram_user_id: int,
    data: str,
) -> str:
    """Process one Approve/Skip tap. Returns the status text to show the user."""
    if allowed_user_id is None or telegram_user_id != allowed_user_id:
        audit.log(
            conn, "approval.unauthorized", detail={"telegram_user_id": telegram_user_id}
        )
        return "⛔ Not authorized."

    decision, item_type, item_id = parse_callback(data)
    if item_type not in ("reply", "follow"):
        return "⚠️ Unknown item."

    if decision == "skip":
        _mark_skip(conn, item_type, item_id)
        return "❌ Skipped."

    if decision != "approve":
        return "⚠️ Unknown action."

    # Approve: mint a fresh token (only call site) and run it through the gate.
    token = mint_approval_token(
        conn,
        item_type=item_type,
        item_id=item_id,
        telegram_user_id=telegram_user_id,
        allowed_user_id=allowed_user_id,
    )
    if token is None:
        return "⛔ Not authorized."

    result = engagement_gate(
        conn, engager, item_type, item_id, token,
        allowed_user_id=allowed_user_id, config=config,
    )
    if result.ok:
        return "✅ Sent." if item_type == "reply" else "✅ Followed."
    return f"⚠️ Not sent ({result.reason})."


# --- PTB wiring (lazy import; not needed by the test suite) -------------------
def build_application(
    *,
    bot_token: str,
    allowed_user_id: int,
    conn_factory: ConnFactory,
    config: Config,
    engager: XEngager,
) -> Any:
    from telegram import Update
    from telegram.ext import (
        Application,
        CallbackQueryHandler,
        CommandHandler,
        ContextTypes,
        filters,
    )

    only_me = filters.User(user_id=allowed_user_id)

    async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        conn = conn_factory()
        try:
            await update.message.reply_text(str(pipeline.status(conn)))
        finally:
            conn.close()

    async def cmd_queue(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        conn = conn_factory()
        try:
            await _send_batch(update.get_bot(), allowed_user_id, conn, config)
        finally:
            conn.close()

    async def cmd_now(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        await cmd_queue(update, _ctx)

    async def cmd_kill(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        conn = conn_factory()
        try:
            cleared = pipeline.pause_and_clear(conn)
            await update.message.reply_text(f"🛑 Paused. Cleared {cleared} queued draft(s).")
        finally:
            conn.close()

    async def cmd_resume(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        conn = conn_factory()
        try:
            pipeline.resume(conn)
            await update.message.reply_text("▶️ Resumed.")
        finally:
            conn.close()

    async def on_callback(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        await query.answer()
        conn = conn_factory()
        try:
            msg = handle_decision(
                conn, config, allowed_user_id, engager,
                update.effective_user.id, query.data,
            )
        finally:
            conn.close()
        await query.edit_message_text(f"{query.message.text}\n\n{msg}")

    app = Application.builder().token(bot_token).build()
    app.add_handler(CommandHandler("status", cmd_status, filters=only_me))
    app.add_handler(CommandHandler("queue", cmd_queue, filters=only_me))
    app.add_handler(CommandHandler("now", cmd_now, filters=only_me))
    app.add_handler(CommandHandler("kill", cmd_kill, filters=only_me))
    app.add_handler(CommandHandler("resume", cmd_resume, filters=only_me))
    app.add_handler(CallbackQueryHandler(on_callback))
    return app


async def _send_batch(bot: Any, chat_id: int, conn: sqlite3.Connection, config: Config) -> None:
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    items = pending_items(conn, config)
    if not items["replies"] and not items["follows"]:
        await bot.send_message(chat_id=chat_id, text="No pending replies or follows. 🎉")
        return

    for r in items["replies"]:
        text = (
            f"💬 Reply to @{r['author_handle']}:\n"
            f"> {r['post_text'][:200]}\n\n"
            f"Draft:\n{r['text']}"
        )
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=make_callback("approve", "reply", r["id"])),
            InlineKeyboardButton("❌ Skip", callback_data=make_callback("skip", "reply", r["id"])),
        ]])
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)

    for f in items["follows"]:
        text = f"👤 Follow @{f['handle']}?\n{f['reason']}"
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=make_callback("approve", "follow", f["id"])),
            InlineKeyboardButton("❌ Skip", callback_data=make_callback("skip", "follow", f["id"])),
        ]])
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)


async def push_batch(
    application: Any, chat_id: int, conn_factory: ConnFactory, config: Config
) -> None:
    """Daily reminder entrypoint: push the pending approval batch to the user."""
    conn = conn_factory()
    try:
        await _send_batch(application.bot, chat_id, conn, config)
    finally:
        conn.close()


async def push_single_reply(bot: Any, chat_id: int, conn: sqlite3.Connection, draft_id: int) -> None:
    """Live notifier: push one fresh reply draft with Approve/Skip (outside the batch)."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    row = conn.execute(
        "SELECT rd.id, rd.text, ro.author_handle, ro.text AS post_text "
        "FROM reply_drafts rd JOIN reply_opportunities ro ON ro.id = rd.opportunity_id "
        "WHERE rd.id = ? AND rd.status = 'draft'",
        (draft_id,),
    ).fetchone()
    if row is None:
        return
    text = (
        f"⚡ LIVE reply to @{row['author_handle']} (fresh post):\n"
        f"> {row['post_text'][:200]}\n\n"
        f"Draft:\n{row['text']}"
    )
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=make_callback("approve", "reply", row["id"])),
        InlineKeyboardButton("❌ Skip", callback_data=make_callback("skip", "reply", row["id"])),
    ]])
    await bot.send_message(chat_id=chat_id, text=text, reply_markup=kb)
