"""Writing-voice baseline distilled from the founder's blog.

The blog (GitHub Pages) is the richest signal for *how* the founder writes. We read
the post HTML straight from the source repo via the GitHub API (already reachable,
no scraping fragility), strip it to text with the standard library, and ask Claude
once to distill a short, reusable **style guide** plus a few real **excerpts**. That
profile is cached in the DB and injected into every drafter as a voice reference —
it shapes tone/word-choice, never length (posts stay short).

``BlogSource`` is a Protocol so tests inject a fake (no network).
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Protocol

from . import audit, db
from .config import Config
from .llm import LLMClient

_SETTING_KEY = "voice_profile"

VOICE_SYSTEM = (
    "You analyze a writer's blog to capture their VOICE for short social posts. "
    "Read the posts and write a concise style guide (5-8 short bullets): tone, "
    "sentence rhythm, vocabulary, attitude, recurring quirks. Focus on voice, not "
    "topics, and do NOT give length guidance (this guides short posts). Output only "
    "the bullets."
)


@dataclass
class BlogPost:
    path: str
    text: str  # stripped plain text


class BlogSource(Protocol):
    def fetch_posts(self) -> list[BlogPost]:
        ...


# --- HTML -> text (stdlib only) ----------------------------------------------
class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = False

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style", "nav", "header", "footer"):
            self._skip = True

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "nav", "header", "footer"):
            self._skip = False

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def strip_html(html: str) -> str:
    parser = _TextExtractor()
    parser.feed(html)
    return "\n".join(parser.parts)


# --- GitHub-backed blog source -----------------------------------------------
class GitHubBlogSource:
    """Reads post HTML files from a GitHub repo path and strips them to text."""

    API = "https://api.github.com"

    def __init__(self, repo: str, path: str, token: str | None = None) -> None:
        self._repo = repo  # "owner/name"
        self._path = path.strip("/")  # e.g. "blog/posts"
        self._token = token

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._token:
            h["Authorization"] = f"Bearer {self._token}"
        return h

    def fetch_posts(self) -> list[BlogPost]:
        import requests  # lazy

        url = f"{self.API}/repos/{self._repo}/contents/{self._path}"
        resp = requests.get(url, headers=self._headers(), timeout=30)
        resp.raise_for_status()
        posts: list[BlogPost] = []
        for item in resp.json():
            name = item.get("name", "")
            if item.get("type") != "file" or not name.endswith((".html", ".htm")):
                continue
            if name.startswith("_"):  # skip templates like _template.html
                continue
            raw = requests.get(item["download_url"], timeout=30)
            if raw.status_code != 200:
                continue
            text = strip_html(raw.text)
            if text.strip():
                posts.append(BlogPost(path=item["path"], text=text))
        return posts


# --- distillation + cache ----------------------------------------------------
def _excerpts(posts: list[BlogPost], *, max_each: int = 160, limit: int = 3) -> list[str]:
    out: list[str] = []
    for p in posts[:limit]:
        s = " ".join(p.text.split())[:max_each].strip()
        if s:
            out.append(s)
    return out


def refresh_voice(
    conn: sqlite3.Connection,
    source: BlogSource,
    llm: LLMClient | None,
    model: str,
    *,
    now: datetime | None = None,
    max_chars: int = 8000,
) -> bool:
    """Fetch blog posts, distill a style guide + excerpts, cache. Returns True if updated."""
    now = now or datetime.now(UTC)
    try:
        posts = source.fetch_posts()
    except Exception as exc:  # noqa: BLE001 — network/parse failures must not crash the loop
        audit.log(conn, "voice.fetch_failed", detail={"error": str(exc)})
        return False
    if not posts:
        return False

    guide = ""
    if llm is not None:
        corpus = "\n\n---\n\n".join(p.text for p in posts)[:max_chars]
        guide = llm.complete(
            model=model, system=VOICE_SYSTEM, user=corpus, max_tokens=400
        ).strip()

    payload = json.dumps(
        {"guide": guide, "excerpts": _excerpts(posts), "fetched_at": now.isoformat()}
    )
    db.set_setting(conn, _SETTING_KEY, payload)
    audit.log(conn, "voice.refreshed", detail={"posts": len(posts), "has_guide": bool(guide)})
    return True


def voice_reference(conn: sqlite3.Connection) -> str:
    """Voice block for drafting prompts: distilled guide + excerpts, or '' if none."""
    raw = db.get_setting(conn, _SETTING_KEY)
    if not raw:
        return ""
    try:
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return ""
    parts: list[str] = []
    if data.get("guide"):
        parts.append("Your writing style (match this voice):\n" + str(data["guide"]))
    if data.get("excerpts"):
        parts.append(
            "Excerpts in your voice:\n" + "\n".join(f"- {e}" for e in data["excerpts"])
        )
    return "\n\n".join(parts)


def voice_is_stale(conn: sqlite3.Connection, *, now: datetime, max_age_days: int) -> bool:
    raw = db.get_setting(conn, _SETTING_KEY)
    if not raw:
        return True
    try:
        data = json.loads(raw)
        fetched = datetime.fromisoformat(data["fetched_at"])
    except (ValueError, KeyError, json.JSONDecodeError):
        return True
    return (now - fetched) > timedelta(days=max_age_days)


def maybe_refresh(
    conn: sqlite3.Connection,
    source: BlogSource | None,
    llm: LLMClient | None,
    config: Config,
    *,
    now: datetime | None = None,
) -> bool:
    """Refresh the voice profile if it's missing/stale and a source is configured."""
    if source is None or not config.voice_blog_repo:
        return False
    now = now or datetime.now(UTC)
    if not voice_is_stale(conn, now=now, max_age_days=config.voice_refresh_days):
        return False
    return refresh_voice(conn, source, llm, config.models.classify, now=now)
