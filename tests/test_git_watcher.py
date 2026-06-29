"""Git watcher: clustering, dedup, cursor advance, scrubbing, classification."""

from __future__ import annotations

from dataclasses import replace

from conftest import FakeCommitSource, always_meaningful
from xgrowth import git_watcher
from xgrowth.config import Repo
from xgrowth.github_client import Commit


def test_first_poll_creates_one_event(conn, config, sample_commits):
    source = FakeCommitSource(sample_commits)
    created = git_watcher.run(conn, config, source, always_meaningful)
    assert len(created) == 1
    rows = conn.execute("SELECT * FROM git_events").fetchall()
    assert len(rows) == 1
    assert rows[0]["is_meaningful"] == 1


def test_dedup_via_cursor(conn, config, sample_commits):
    source = FakeCommitSource(sample_commits)
    git_watcher.run(conn, config, source, always_meaningful)
    # Second poll: cursor now points at newest sha, so no new commits -> no event.
    created2 = git_watcher.run(conn, config, source, always_meaningful)
    assert created2 == []
    assert conn.execute("SELECT COUNT(*) FROM git_events").fetchone()[0] == 1


def test_dedup_key_blocks_reprocessing_same_set(conn, config, sample_commits):
    repo = config.repos[0]
    source = FakeCommitSource(sample_commits)
    git_watcher.poll_repo(conn, repo, source, always_meaningful, config)
    # Force the cursor back so the same commit set is seen again; dedup_key must block it.
    conn.execute("UPDATE repos SET last_seen_sha = NULL WHERE full_name = ?", (repo.full_name,))
    conn.commit()
    event_id = git_watcher.poll_repo(conn, repo, source, always_meaningful, config)
    assert event_id is None
    assert conn.execute("SELECT COUNT(*) FROM git_events").fetchone()[0] == 1


def test_summary_is_scrubbed(conn, config):
    leaky = [
        Commit(
            sha="c1",
            message="add config\nAPI_SECRET=topsecretvalue123",
            date="2026-06-22T10:00:00Z",
            author="zarfix123",
            files=[".env"],
        )
    ]
    git_watcher.run(conn, config, FakeCommitSource(leaky), always_meaningful)
    summary = conn.execute("SELECT summary FROM git_events").fetchone()["summary"]
    assert "topsecretvalue123" not in summary


def test_heuristic_classifier_marks_trivial_not_meaningful():
    summary = "- wip\n- typo fix\n- bump version"
    result = git_watcher.heuristic_classifier(summary, ["AI"])
    assert result.meaningful is False


def test_heuristic_classifier_marks_real_work_meaningful():
    summary = "- add reply ranking engine\nfiles: monitor.py"
    result = git_watcher.heuristic_classifier(summary, ["AI"])
    assert result.meaningful is True


def test_dedup_key_is_order_independent():
    a = git_watcher.dedup_key("o/r", ["x", "y", "z"])
    b = git_watcher.dedup_key("o/r", ["z", "y", "x"])
    assert a == b


def test_watch_all_repos_polls_discovered_repos(conn, config, sample_commits):
    cfg = replace(config, watch_all_repos=True)
    discovered = [Repo("zarfix123", "alpha"), Repo("zarfix123", "beta")]
    source = FakeCommitSource(sample_commits, repos=discovered)
    created = git_watcher.run(conn, cfg, source, always_meaningful)
    # one event per discovered repo (each has the same fake commits, distinct dedup_key per repo)
    assert len(created) == 2
    repos_seen = {r["repo"] for r in conn.execute("SELECT repo FROM git_events").fetchall()}
    assert repos_seen == {"zarfix123/alpha", "zarfix123/beta"}
    # it asked the source to enumerate, with a pushed-since window
    assert any(c.get("list_repos") and c["since_pushed"] for c in source.calls)


def test_explicit_repos_mode_does_not_enumerate(conn, config, sample_commits):
    source = FakeCommitSource(sample_commits, repos=[Repo("x", "y")])
    git_watcher.run(conn, config, source, always_meaningful)  # watch_all_repos False
    assert not any(c.get("list_repos") for c in source.calls)


def test_commit_link_public_private_homepage(conn, config, sample_commits):
    cfg = replace(config, watch_all_repos=True)
    repos = [
        Repo("zarfix123", "pub"),                                # public, no homepage -> github
        Repo("Hadeva-Dev", "Tolus", private=True),               # private, no homepage -> "" (no link)
        Repo("zarfix123", "site", homepage="https://tolus.dev"),  # homepage wins
    ]
    git_watcher.run(conn, cfg, FakeCommitSource(sample_commits, repos=repos), always_meaningful)
    links = {r["repo"]: r["link"] for r in conn.execute("SELECT repo, link FROM git_events").fetchall()}
    assert links["zarfix123/pub"] == "https://github.com/zarfix123/pub"
    assert links["Hadeva-Dev/Tolus"] == ""           # private -> the poster adds no self-reply
    assert links["zarfix123/site"] == "https://tolus.dev"


def test_repo_links_override_beats_homepage(conn, config, sample_commits):
    # A private repo whose GitHub homepage points at a login-gated deploy: the
    # hard-locked repo_links override must win and post the canonical public URL.
    cfg = replace(
        config,
        watch_all_repos=True,
        repo_links={"Hadeva-Dev/Tolus": "https://www.tolus.dev"},
    )
    repos = [
        Repo("Hadeva-Dev", "Tolus", private=True,
             homepage="https://vercel.com/sso/access/request?next=tolus-tolus.vercel.app"),
    ]
    git_watcher.run(conn, cfg, FakeCommitSource(sample_commits, repos=repos), always_meaningful)
    link = conn.execute("SELECT link FROM git_events").fetchone()["link"]
    assert link == "https://www.tolus.dev"  # override beats the gated homepage
