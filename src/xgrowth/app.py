"""Entrypoint: Telegram approval bot + AsyncIOScheduler cron loops + FastAPI admin.

Run with:  PYTHONPATH=src python -m xgrowth.app

Async-first: the main asyncio loop owns the python-telegram-bot Application and an
AsyncIOScheduler; blocking job bodies run in ``asyncio.to_thread`` so they never
stall the bot. The FastAPI admin surface (health/status/kill/resume) is served by
uvicorn in a daemon thread.

Jobs:
  - watch_cycle  (every 30 min):                 repos -> drafts -> schedule
  - post_tick    (every 1 min):                  publish due original drafts
  - monitor_scan (every monitor_scan_interval):  find + draft reply opportunities
  - reply_reminder (daily, jittered):            push the approval batch to Telegram

All engagement (replies/follows) flows only through the engagement gate, triggered
by a human Telegram approval — see engagement.py.
"""

from __future__ import annotations

import asyncio
import logging
import random
import threading
from datetime import datetime

from dotenv import load_dotenv

from . import (
    analytics,
    db,
    live_reply,
    monitor,
    news_content_gen,
    news_watcher,
    pipeline,
    reply_drafter,
    x_auth,
)
from . import poster as poster_mod
from . import scheduler as scheduler_mod
from .config import Config, Secrets, load_config, load_secrets
from .engagement import DryRunXEngager, RealXEngager, XEngager
from .git_watcher import Classifier, heuristic_classifier, make_llm_classifier
from .github_client import GitHubCommitSource
from .llm import AnthropicClient
from .news_source import HackerNewsSource
from .news_watcher import make_news_classifier, news_heuristic_classifier
from .scheduler import parse_windows
from .x_client import DryRunXPoster, RealXPoster, XPoster
from .x_read import FakeXReader, RealXReader, XReader

logger = logging.getLogger("xgrowth")


def _now() -> datetime:
    return datetime.now().astimezone()


def build_runtime(secrets: Secrets, config: Config, conn_factory):
    """Construct source/classifier/llm/poster/reader/engager with safe fallbacks."""
    source = GitHubCommitSource(secrets.github_token)

    llm = None
    classifier: Classifier = heuristic_classifier
    if secrets.anthropic_api_key:
        llm = AnthropicClient(secrets.anthropic_api_key)
        classifier = make_llm_classifier(llm, config.models.classify)

    live_x = bool(secrets.x_access_token) and not secrets.dry_run

    if live_x:
        # One provider keeps a valid OAuth2 user token across all three surfaces,
        # refreshing + persisting it (DB) as the ~2h access token expires.
        provider = x_auth.XTokenProvider(
            client_id=secrets.x_client_id,
            client_secret=secrets.x_client_secret,
            conn_factory=conn_factory,
            seed=x_auth.build_seed(secrets.x_access_token, secrets.x_refresh_token),
        )
        poster: XPoster = RealXPoster(provider.token)
        reader: XReader = RealXReader(provider.token, conn=conn_factory())
        engager: XEngager = RealXEngager(provider.token)
        if not secrets.x_refresh_token:
            logger.warning(
                "X: no refresh token (X_REFRESH_TOKEN). The access token will expire in "
                "~2h and posting will stop. Run scripts/x_oauth.py to mint a refreshable token."
            )
    else:
        poster = DryRunXPoster()
        reader = FakeXReader()
        engager = DryRunXEngager()
        logger.info("X surfaces in DRY-RUN: no live reads/posts/engagement.")
    return source, classifier, llm, poster, reader, engager


def build_admin_app(secrets: Secrets):
    """FastAPI admin surface (served by uvicorn in a daemon thread)."""
    from fastapi import FastAPI

    app = FastAPI(title="X Growth Engine", version="0.2.0")

    def open_conn():
        return db.connect(secrets.db_path)

    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": "0.2.0"}

    @app.get("/status")
    def status() -> dict:
        c = open_conn()
        try:
            return pipeline.status(c)
        finally:
            c.close()

    @app.post("/admin/kill")
    def kill() -> dict:
        c = open_conn()
        try:
            return {"paused": True, "cleared": pipeline.pause_and_clear(c)}
        finally:
            c.close()

    @app.post("/admin/resume")
    def resume() -> dict:
        c = open_conn()
        try:
            pipeline.resume(c)
            return {"paused": False}
        finally:
            c.close()

    return app


def _start_admin_thread(secrets: Secrets) -> None:
    import uvicorn

    cfg = uvicorn.Config(
        build_admin_app(secrets), host=secrets.host, port=secrets.port, log_level="warning"
    )
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True, name="admin").start()
    logger.info("Admin server on http://%s:%d", secrets.host, secrets.port)


async def main_async() -> None:
    load_dotenv()
    secrets = load_secrets()
    config = load_config(secrets.config_path)

    init = db.connect(secrets.db_path)
    db.init_db(init)
    init.close()

    def conn_factory():
        return db.connect(secrets.db_path)

    source, classifier, llm, poster, reader, engager = build_runtime(
        secrets, config, conn_factory
    )

    # AI-news source + classifier (Algolia HN needs no secret; classify uses Haiku).
    news_source = HackerNewsSource()
    news_classifier: Classifier = (
        make_news_classifier(llm, config.models.classify) if llm else news_heuristic_classifier
    )

    # ---- blocking job bodies (run via to_thread) ----
    def _watch_cycle() -> None:
        c = conn_factory()
        try:
            ins = analytics.insights(c)  # feedback into drafting + timing
            pipeline.watch_and_draft(
                c, config, source, classifier, llm,
                now=_now(), hints=ins.hint_text or None, preferred_hours=ins.best_hours or None,
            )
        finally:
            c.close()

    def _post_tick() -> None:
        c = conn_factory()
        try:
            poster_mod.publish_due(c, config, poster, now=_now())
        finally:
            c.close()

    def _monitor_scan() -> None:
        c = conn_factory()
        try:
            monitor.scan(c, config, reader, llm, now=_now())
            reply_drafter.draft_pending(c, config, llm)
        finally:
            c.close()

    def _analytics_pull() -> None:
        c = conn_factory()
        try:
            analytics.pull(c, config, reader, now=_now())
        finally:
            c.close()

    def _ai_news_tick() -> None:
        c = conn_factory()
        try:
            ins = analytics.insights(c)  # same soft feedback as commit posts
            news_watcher.scan(c, config, news_source, news_classifier, now=_now())
            news_content_gen.generate_news_drafts(
                c, config, llm, now=_now(), hints=ins.hint_text or None
            )
            scheduler_mod.schedule_pending(
                c, config, now=_now(), preferred_hours=ins.best_hours or None
            )
        finally:
            c.close()

    async def watch_cycle() -> None:
        await asyncio.to_thread(_watch_cycle)

    async def post_tick() -> None:
        await asyncio.to_thread(_post_tick)

    async def monitor_scan() -> None:
        await asyncio.to_thread(_monitor_scan)

    async def analytics_pull() -> None:
        await asyncio.to_thread(_analytics_pull)

    async def ai_news_tick() -> None:
        await asyncio.to_thread(_ai_news_tick)

    # ---- Telegram bot (optional; only if configured) ----
    application = None
    live_reply_tick = None
    if secrets.telegram_bot_token and secrets.telegram_allowed_user_id:
        from .telegram_bot import build_application, push_batch, push_single_reply

        application = build_application(
            bot_token=secrets.telegram_bot_token,
            allowed_user_id=secrets.telegram_allowed_user_id,
            conn_factory=conn_factory,
            config=config,
            engager=engager,
        )
        chat_id = secrets.telegram_allowed_user_id

        async def reply_reminder() -> None:
            # Jitter within the reminder window so the session time isn't mechanical.
            windows = parse_windows([config.reply_reminder_window])
            if windows:
                span = (
                    windows[0].end.hour * 60 + windows[0].end.minute
                    - windows[0].start.hour * 60 - windows[0].start.minute
                )
                await asyncio.sleep(random.randint(0, max(span, 0)) * 60)
            await push_batch(application, chat_id, conn_factory, config)

        async def live_reply_tick() -> None:
            def _scan() -> list[int]:
                c = conn_factory()
                try:
                    return live_reply.scan_live(c, config, reader, llm, now=_now())
                finally:
                    c.close()

            new_ids = await asyncio.to_thread(_scan)
            for draft_id in new_ids:
                c = conn_factory()
                try:
                    await push_single_reply(application.bot, chat_id, c, draft_id)
                finally:
                    c.close()
    else:
        logger.info("Telegram not configured; approval bot disabled.")
        reply_reminder = None

    # ---- scheduler ----
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = AsyncIOScheduler()
    sched.add_job(watch_cycle, "interval", minutes=30, id="watch_cycle")
    sched.add_job(post_tick, "interval", minutes=1, id="post_tick")
    sched.add_job(
        monitor_scan, "interval", minutes=config.monitor_scan_interval_minutes, id="monitor_scan"
    )
    sched.add_job(
        analytics_pull, "interval", hours=config.analytics_pull_interval_hours, id="analytics_pull"
    )
    if config.ai_news_enabled:
        sched.add_job(
            ai_news_tick, "interval", hours=config.ai_news_interval_hours, id="ai_news_tick"
        )
    if reply_reminder is not None:
        windows = parse_windows([config.reply_reminder_window])
        start = windows[0].start if windows else datetime.now().time().replace(hour=12, minute=0)
        sched.add_job(
            reply_reminder,
            CronTrigger(hour=start.hour, minute=start.minute),
            id="reply_reminder",
        )
    if live_reply_tick is not None:
        sched.add_job(
            live_reply_tick,
            "interval",
            minutes=config.live_reply_interval_minutes,
            id="live_reply_tick",
        )

    _start_admin_thread(secrets)
    sched.start()
    logger.info("Scheduler started.")

    if application is not None:
        await application.initialize()
        await application.start()
        await application.updater.start_polling()
        logger.info("Telegram bot polling.")

    try:
        await asyncio.Event().wait()  # run forever
    finally:
        if application is not None:
            await application.updater.stop()
            await application.stop()
            await application.shutdown()
        sched.shutdown(wait=False)


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
