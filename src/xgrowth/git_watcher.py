"""Git watcher: turn raw commit activity into deduplicated, meaningful change events.

Per repo: poll commits since the cursor, cluster the new ones, scrub them, ask the
classifier whether they're a meaningful + on-topic unit of work, and persist a
``git_events`` row keyed by a dedup hash so the same work is never posted twice.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import audit, scrub
from .config import Config, Repo
from .github_client import Commit, CommitSource
from .llm import LLMClient

# A classifier judges a scrubbed change summary. Injectable for tests/offline use.
Classifier = Callable[[str, list[str]], "ClassifyResult"]

_TRIVIAL_HINTS = ("wip", "typo", "bump", "merge branch", "merge pull", "lint", "format")


@dataclass
class ClassifyResult:
    meaningful: bool
    topic: str
    summary: str


def dedup_key(repo_full_name: str, shas: list[str]) -> str:
    payload = repo_full_name + "|" + "|".join(sorted(shas))
    return hashlib.sha256(payload.encode()).hexdigest()


def build_summary(commits: list[Commit]) -> str:
    """Build a scrubbed, human-readable summary from commit messages + file paths."""
    lines: list[str] = []
    for c in commits:
        first_line = c.message.strip().splitlines()[0] if c.message.strip() else c.sha[:7]
        lines.append(f"- {first_line}")
    files = sorted({f for c in commits for f in c.files if f})
    if files:
        shown = ", ".join(files[:12])
        lines.append(f"files: {shown}")
    raw = "\n".join(lines)
    return scrub.scrub_text(raw).text


def heuristic_classifier(summary: str, topic_clusters: list[str]) -> ClassifyResult:
    """Offline fallback: meaningful unless it looks purely trivial."""
    low = summary.lower()
    meaningful = not all(
        any(h in line for h in _TRIVIAL_HINTS) or not line.strip()
        for line in low.splitlines()
        if line.startswith("- ")
    )
    topic = topic_clusters[0] if topic_clusters else "general"
    return ClassifyResult(meaningful=meaningful, topic=topic, summary=summary)


def make_llm_classifier(llm: LLMClient, model: str) -> Classifier:
    """Classifier backed by the cheap (Haiku) model."""

    def classify(summary: str, topic_clusters: list[str]) -> ClassifyResult:
        system = (
            "You judge whether a batch of code changes is worth a 'building in "
            "public' post. Meaningful = a shipped feature, a milestone, a notable "
            "fix, or a clear narrative. Not meaningful = pure chores (typos, lint, "
            "version bumps, merges) with no story. Respond ONLY with compact JSON: "
            '{"meaningful": bool, "topic": str}. topic must be one of the user\'s '
            "clusters or 'general'."
        )
        user = (
            f"Topic clusters: {topic_clusters}\n\nChange summary (already scrubbed):\n{summary}"
        )
        text = llm.complete(model=model, system=system, user=user, max_tokens=200)
        try:
            data = json.loads(text[text.index("{") : text.rindex("}") + 1])
            return ClassifyResult(
                meaningful=bool(data.get("meaningful", False)),
                topic=str(data.get("topic", "general")),
                summary=summary,
            )
        except (ValueError, json.JSONDecodeError):
            # On any parse failure, fall back to the conservative heuristic.
            return heuristic_classifier(summary, topic_clusters)

    return classify


def _get_cursor(conn: sqlite3.Connection, repo: Repo) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT last_polled_at, last_seen_sha FROM repos WHERE full_name = ?",
        (repo.full_name,),
    ).fetchone()
    if row is None:
        return None, None
    return row["last_polled_at"], row["last_seen_sha"]


def _set_cursor(conn: sqlite3.Connection, repo: Repo, polled_at: str, last_sha: str) -> None:
    conn.execute(
        "INSERT INTO repos(full_name, last_polled_at, last_seen_sha) VALUES(?,?,?) "
        "ON CONFLICT(full_name) DO UPDATE SET "
        "last_polled_at = excluded.last_polled_at, last_seen_sha = excluded.last_seen_sha",
        (repo.full_name, polled_at, last_sha),
    )
    conn.commit()


def poll_repo(
    conn: sqlite3.Connection,
    repo: Repo,
    source: CommitSource,
    classifier: Classifier,
    config: Config,
    *,
    default_lookback_hours: int = 48,
) -> int | None:
    """Poll one repo. Returns the new git_event id, or None if nothing new/meaningful."""
    last_polled_at, last_seen_sha = _get_cursor(conn, repo)
    since = last_polled_at or (
        datetime.now(UTC) - timedelta(hours=default_lookback_hours)
    ).isoformat()

    commits = source.list_commits(repo, since=since, author=config.github_author or None)
    # Drop anything at/older than the last seen sha (GitHub `since` is time-based,
    # which can re-include the boundary commit).
    new_commits: list[Commit] = []
    for c in commits:
        if c.sha == last_seen_sha:
            break
        new_commits.append(c)

    now = datetime.now(UTC).isoformat()
    if not new_commits:
        _set_cursor(conn, repo, now, last_seen_sha or "")
        return None

    newest_sha = new_commits[0].sha
    shas = [c.sha for c in new_commits]
    key = dedup_key(repo.full_name, shas)

    existing = conn.execute(
        "SELECT id FROM git_events WHERE dedup_key = ?", (key,)
    ).fetchone()
    if existing is not None:
        _set_cursor(conn, repo, now, newest_sha)
        return None

    summary = build_summary(new_commits)
    result = classifier(summary, config.topic_clusters)

    cur = conn.execute(
        "INSERT INTO git_events(repo, commit_shas, summary, dedup_key, is_meaningful, "
        "topic, consumed, created_at) VALUES(?,?,?,?,?,?,0,?)",
        (
            repo.full_name,
            json.dumps(shas),
            result.summary,
            key,
            1 if result.meaningful else 0,
            result.topic,
            now,
        ),
    )
    conn.commit()
    event_id = int(cur.lastrowid)
    _set_cursor(conn, repo, now, newest_sha)
    audit.log(
        conn,
        "git_event.created",
        entity_type="git_event",
        entity_id=event_id,
        detail={"repo": repo.full_name, "commits": len(shas), "meaningful": result.meaningful},
    )
    return event_id


def run(
    conn: sqlite3.Connection,
    config: Config,
    source: CommitSource,
    classifier: Classifier,
) -> list[int]:
    """Poll every configured repo. Returns the list of new git_event ids created."""
    created: list[int] = []
    for repo in config.repos:
        event_id = poll_repo(conn, repo, source, classifier, config)
        if event_id is not None:
            created.append(event_id)
    return created
