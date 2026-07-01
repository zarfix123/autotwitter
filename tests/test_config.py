"""Config parsing: defaults + the engagement master switch."""

from __future__ import annotations

from xgrowth.config import config_from_dict

_MIN = {"repos": [], "github_author": "x"}


def test_engagement_enabled_defaults_true():
    assert config_from_dict(_MIN).engagement_enabled is True


def test_engagement_can_be_disabled():
    assert config_from_dict({**_MIN, "engagement_enabled": False}).engagement_enabled is False


def test_news_defaults_are_loosened():
    cfg = config_from_dict(_MIN)
    assert cfg.news_min_points == 15
    assert cfg.news_item_max_age_hours == 48
