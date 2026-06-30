"""Configuration loading.

All tunable behavior lives in a YAML file (see config.example.yaml) plus secrets
from the environment (.env). Modules read a typed `Config` object; nothing is
hardcoded.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str
    private: bool = False
    homepage: str | None = None  # public project URL (GitHub "Website" field), if set

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class Models:
    classify: str = "claude-haiku-4-5"
    draft: str = "claude-sonnet-4-6"
    polish: str = "claude-opus-4-8"


@dataclass(frozen=True)
class Config:
    repos: list[Repo]
    github_author: str
    topic_clusters: list[str]
    target_accounts: list[str]
    keywords: list[str]
    voice_samples: list[str]
    posting_windows: list[str]
    posts_per_day: int = 2
    min_post_spacing_minutes: int = 180
    post_jitter_minutes: int = 25
    daily_reply_queue_size: int = 5
    max_follows_per_day: int = 2
    x_premium: bool = False
    weekly_cost_cap_usd: float = 15.0
    # Phase 2 (growth engine) tunables.
    reply_reminder_window: str = "12:00-13:00"
    monitor_scan_interval_minutes: int = 30
    opportunity_max_age_minutes: int = 120
    follow_min_spacing_minutes: int = 240
    # Phase 3 (feedback & timeliness) tunables.
    analytics_pull_interval_hours: int = 6
    live_reply_interval_minutes: int = 10
    live_reply_max_age_minutes: int = 15
    live_reply_min_followers: int = 0
    # AI-news content source (auto-posted, shares posts_per_day).
    ai_news_enabled: bool = False
    ai_news_interval_hours: int = 6
    ai_news_max_per_day: int = 1
    news_min_points: int = 15
    news_item_max_age_hours: int = 48
    ai_news_style: str = "mix"  # mix | opinion | tie_in
    # Commit selection (windowed best-pick) + content mix.
    commit_window_days: int = 7
    commit_posts_per_day: int = 1
    # Watch-all-repos: auto-discover every repo you own (incl. private + future ones)
    # instead of using the explicit `repos` list. Requires a GitHub token.
    watch_all_repos: bool = False
    watch_all_repos_days: int = 14  # only poll repos pushed within this many days
    # Writing voice distilled from the blog (empty repo = use static voice_samples).
    voice_blog_repo: str = ""          # "owner/name", e.g. zarfix123/zarfix123.github.io
    voice_blog_path: str = "blog/posts"
    voice_refresh_days: int = 7
    # Hard-locked public link per repo (full_name -> URL). Overrides auto-detection
    # (GitHub homepage / repo URL) — use when a repo's homepage points somewhere wrong.
    repo_links: dict[str, str] = field(default_factory=dict)
    models: Models = field(default_factory=Models)

    # ---- validation helpers -------------------------------------------------
    def __post_init__(self) -> None:
        if self.posts_per_day < 1:
            raise ValueError("posts_per_day must be >= 1")
        if self.posts_per_day > 4:
            # Posting more dilutes reach; the plan caps the configurable max at 4.
            raise ValueError("posts_per_day must be <= 4 (more dilutes reach)")
        if self.daily_reply_queue_size < 1 or self.daily_reply_queue_size > 15:
            raise ValueError("daily_reply_queue_size must be in 1..15")
        if self.min_post_spacing_minutes < 30:
            raise ValueError("min_post_spacing_minutes must be >= 30")
        if not (0 <= self.ai_news_max_per_day <= self.posts_per_day):
            raise ValueError("ai_news_max_per_day must be in 0..posts_per_day")
        if self.ai_news_style not in ("mix", "opinion", "tie_in"):
            raise ValueError("ai_news_style must be one of: mix, opinion, tie_in")
        if not (0 <= self.commit_posts_per_day <= self.posts_per_day):
            raise ValueError("commit_posts_per_day must be in 0..posts_per_day")
        if self.commit_window_days < 1:
            raise ValueError("commit_window_days must be >= 1")


@dataclass(frozen=True)
class Secrets:
    anthropic_api_key: str | None
    github_token: str | None
    x_client_id: str | None
    x_client_secret: str | None
    x_access_token: str | None
    x_refresh_token: str | None
    telegram_bot_token: str | None
    telegram_allowed_user_id: int | None
    db_path: str
    config_path: str
    dry_run: bool
    host: str
    port: int


def load_config(path: str | os.PathLike[str] | None = None) -> Config:
    """Load and validate the YAML config."""
    cfg_path = Path(path or os.environ.get("XGROWTH_CONFIG_PATH", "config.yaml"))
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Config not found at {cfg_path}. Copy config.example.yaml to config.yaml."
        )
    raw = yaml.safe_load(cfg_path.read_text()) or {}
    return config_from_dict(raw)


def config_from_dict(raw: dict) -> Config:
    """Build a Config from a plain dict (used by load_config and tests)."""
    repos = [Repo(owner=r["owner"], name=r["name"]) for r in raw.get("repos", [])]
    models_raw = raw.get("models", {}) or {}
    models = Models(
        classify=models_raw.get("classify", Models.classify),
        draft=models_raw.get("draft", Models.draft),
        polish=models_raw.get("polish", Models.polish),
    )
    return Config(
        repos=repos,
        github_author=raw.get("github_author", ""),
        topic_clusters=list(raw.get("topic_clusters", [])),
        target_accounts=list(raw.get("target_accounts", [])),
        keywords=list(raw.get("keywords", [])),
        voice_samples=list(raw.get("voice_samples", [])),
        posting_windows=list(raw.get("posting_windows", [])),
        posts_per_day=int(raw.get("posts_per_day", 2)),
        min_post_spacing_minutes=int(raw.get("min_post_spacing_minutes", 180)),
        post_jitter_minutes=int(raw.get("post_jitter_minutes", 25)),
        daily_reply_queue_size=int(raw.get("daily_reply_queue_size", 5)),
        max_follows_per_day=int(raw.get("max_follows_per_day", 2)),
        x_premium=bool(raw.get("x_premium", False)),
        weekly_cost_cap_usd=float(raw.get("weekly_cost_cap_usd", 15.0)),
        reply_reminder_window=str(raw.get("reply_reminder_window", "12:00-13:00")),
        monitor_scan_interval_minutes=int(raw.get("monitor_scan_interval_minutes", 30)),
        opportunity_max_age_minutes=int(raw.get("opportunity_max_age_minutes", 120)),
        follow_min_spacing_minutes=int(raw.get("follow_min_spacing_minutes", 240)),
        analytics_pull_interval_hours=int(raw.get("analytics_pull_interval_hours", 6)),
        live_reply_interval_minutes=int(raw.get("live_reply_interval_minutes", 10)),
        live_reply_max_age_minutes=int(raw.get("live_reply_max_age_minutes", 15)),
        live_reply_min_followers=int(raw.get("live_reply_min_followers", 0)),
        ai_news_enabled=bool(raw.get("ai_news_enabled", False)),
        ai_news_interval_hours=int(raw.get("ai_news_interval_hours", 6)),
        ai_news_max_per_day=int(raw.get("ai_news_max_per_day", 1)),
        news_min_points=int(raw.get("news_min_points", 15)),
        news_item_max_age_hours=int(raw.get("news_item_max_age_hours", 48)),
        ai_news_style=str(raw.get("ai_news_style", "mix")),
        commit_window_days=int(raw.get("commit_window_days", 7)),
        commit_posts_per_day=int(raw.get("commit_posts_per_day", 1)),
        watch_all_repos=bool(raw.get("watch_all_repos", False)),
        watch_all_repos_days=int(raw.get("watch_all_repos_days", 14)),
        voice_blog_repo=str(raw.get("voice_blog_repo", "")),
        voice_blog_path=str(raw.get("voice_blog_path", "blog/posts")),
        voice_refresh_days=int(raw.get("voice_refresh_days", 7)),
        repo_links=dict(raw.get("repo_links", {}) or {}),
        models=models,
    )


def _int_or_none(value: str | None) -> int | None:
    if value is None or value.strip() == "":
        return None
    return int(value)


def load_secrets() -> Secrets:
    """Read secrets and runtime knobs from the environment."""
    return Secrets(
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        github_token=os.environ.get("GITHUB_TOKEN"),
        x_client_id=os.environ.get("X_CLIENT_ID"),
        x_client_secret=os.environ.get("X_CLIENT_SECRET"),
        x_access_token=os.environ.get("X_ACCESS_TOKEN"),
        x_refresh_token=os.environ.get("X_REFRESH_TOKEN"),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        telegram_allowed_user_id=_int_or_none(os.environ.get("TELEGRAM_ALLOWED_USER_ID")),
        db_path=os.environ.get("XGROWTH_DB_PATH", "data/xgrowth.db"),
        config_path=os.environ.get("XGROWTH_CONFIG_PATH", "config.yaml"),
        dry_run=os.environ.get("XGROWTH_DRY_RUN", "1") not in ("0", "false", "False", ""),
        host=os.environ.get("XGROWTH_HOST", "127.0.0.1"),
        port=int(os.environ.get("XGROWTH_PORT", "8080")),
    )
