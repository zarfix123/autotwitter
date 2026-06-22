"""End-to-end Phase 1 cycle with fakes, plus kill-switch behavior."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from conftest import FakeCommitSource
from xgrowth import pipeline
from xgrowth.git_watcher import make_llm_classifier
from xgrowth.x_client import DryRunXPoster

NOW = datetime(2026, 6, 22, 8, 0, tzinfo=UTC)


def test_full_cycle_drafts_and_schedules(conn, config, fake_llm, sample_commits):
    source = FakeCommitSource(sample_commits)
    classifier = make_llm_classifier(fake_llm, config.models.classify)
    poster = DryRunXPoster()

    result = pipeline.run_cycle(conn, config, source, classifier, fake_llm, poster, now=NOW)
    assert len(result.new_events) == 1
    assert len(result.new_drafts) == 1
    assert len(result.scheduled) == 1
    # Scheduled into a future window, so nothing is published at 08:00.
    assert result.published == []


def test_second_cycle_publishes_when_due(conn, config, fake_llm, sample_commits):
    source = FakeCommitSource(sample_commits)
    classifier = make_llm_classifier(fake_llm, config.models.classify)
    poster = DryRunXPoster()

    pipeline.run_cycle(conn, config, source, classifier, fake_llm, poster, now=NOW)
    # A day later the scheduled post is due.
    later = NOW + timedelta(days=1)
    result2 = pipeline.run_cycle(conn, config, source, classifier, fake_llm, poster, now=later)
    assert len(result2.published) == 1


def test_kill_switch_clears_queue(conn, config, fake_llm, sample_commits):
    source = FakeCommitSource(sample_commits)
    classifier = make_llm_classifier(fake_llm, config.models.classify)
    pipeline.run_cycle(conn, config, source, classifier, fake_llm, DryRunXPoster(), now=NOW)

    cleared = pipeline.pause_and_clear(conn)
    assert cleared >= 1
    assert pipeline.status(conn)["paused"] is True
    assert pipeline.status(conn)["drafts_scheduled"] == 0


def test_status_shape(conn, config):
    s = pipeline.status(conn)
    assert set(s) >= {
        "paused",
        "git_events",
        "drafts_pending",
        "drafts_scheduled",
        "drafts_posted",
        "weekly_spend_usd",
    }
