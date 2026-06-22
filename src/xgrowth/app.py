"""Entrypoint: FastAPI admin surface + APScheduler cron loops.

Run with:  python -m xgrowth.app    (or uvicorn xgrowth.app:create_app --factory)

Phase 1 jobs:
  - watch_cycle  (default every 30 min): poll repos -> draft -> schedule
  - post_tick    (default every 1 min):  publish drafts whose time has come

Engagement (replies/follows) is Phase 2 and is intentionally absent here.
"""

from __future__ import annotations

import logging
from datetime import datetime

from dotenv import load_dotenv

from . import db, pipeline
from .config import Config, Secrets, load_config, load_secrets
from .git_watcher import Classifier, heuristic_classifier, make_llm_classifier
from .github_client import GitHubCommitSource
from .llm import AnthropicClient
from .x_client import DryRunXPoster, RealXPoster, XPoster

logger = logging.getLogger("xgrowth")


def _now():
    # Local-time-aware "now" so posting windows match the instance timezone.
    return datetime.now().astimezone()


def _build_runtime(secrets: Secrets, config: Config):
    """Construct source/classifier/llm/poster from secrets, with safe fallbacks."""
    source = GitHubCommitSource(secrets.github_token)

    llm = None
    classifier: Classifier = heuristic_classifier
    if secrets.anthropic_api_key:
        # The LLM client logs cost against a connection opened per call below;
        # we pass None here and rely on the per-call connection in jobs instead.
        llm = AnthropicClient(secrets.anthropic_api_key)
        classifier = make_llm_classifier(llm, config.models.classify)

    poster: XPoster
    if secrets.dry_run or not secrets.x_access_token:
        logger.info("X poster running in DRY-RUN mode (no live tweets).")
        poster = DryRunXPoster()
    else:
        poster = RealXPoster(secrets.x_access_token)

    return source, classifier, llm, poster


def create_app():
    from fastapi import FastAPI

    load_dotenv()
    secrets = load_secrets()
    config = load_config(secrets.config_path)

    # Initialize DB.
    conn = db.connect(secrets.db_path)
    db.init_db(conn)
    conn.close()

    source, classifier, llm, poster = _build_runtime(secrets, config)

    app = FastAPI(title="X Growth Engine", version="0.1.0")

    def open_conn():
        return db.connect(secrets.db_path)

    # ---- jobs ----
    def watch_cycle() -> None:
        c = open_conn()
        try:
            events, drafts, scheduled = pipeline.watch_and_draft(
                c, config, source, classifier, llm, now=_now()
            )
            if events or drafts or scheduled:
                logger.info(
                    "watch_cycle: %d events, %d drafts, %d scheduled",
                    len(events), len(drafts), len(scheduled),
                )
        finally:
            c.close()

    def post_tick() -> None:
        c = open_conn()
        try:
            from . import poster as poster_mod

            published = poster_mod.publish_due(c, config, poster, now=_now())
            if published:
                logger.info("post_tick: published %d draft(s)", len(published))
        finally:
            c.close()

    # ---- admin endpoints ----
    @app.get("/health")
    def health() -> dict:
        return {"ok": True, "version": "0.1.0"}

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
            cleared = pipeline.pause_and_clear(c)
            return {"paused": True, "cleared": cleared}
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

    # ---- scheduler ----
    from apscheduler.schedulers.background import BackgroundScheduler

    sched = BackgroundScheduler()
    sched.add_job(watch_cycle, "interval", minutes=30, id="watch_cycle")
    sched.add_job(post_tick, "interval", minutes=1, id="post_tick")

    @app.on_event("startup")
    def _start() -> None:
        sched.start()
        logger.info("APScheduler started (watch_cycle/30m, post_tick/1m).")

    @app.on_event("shutdown")
    def _stop() -> None:
        sched.shutdown(wait=False)

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=logging.INFO)
    secrets = load_secrets()
    uvicorn.run(create_app(), host=secrets.host, port=secrets.port)


if __name__ == "__main__":
    main()
