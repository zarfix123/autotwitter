"""Voice baseline: HTML strip, distill + cache, staleness, safe failure."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from xgrowth import voice
from xgrowth.voice import BlogPost, strip_html

NOW = datetime(2026, 6, 24, 12, 0, tzinfo=UTC)


class FakeBlogSource:
    def __init__(self, posts):
        self.posts = posts

    def fetch_posts(self):
        return list(self.posts)


class FakeVoiceLLM:
    def __init__(self, guide="- candid\n- direct"):
        self._guide = guide
        self.calls = []

    def complete(self, *, model, system, user, max_tokens=1024):
        self.calls.append({"system": system, "user": user})
        return self._guide


def test_strip_html_keeps_prose_drops_scripts():
    html = (
        "<html><head><style>p{color:red}</style></head><body>"
        "<h1>Title</h1><p>real prose here</p><script>evil()</script></body></html>"
    )
    out = strip_html(html)
    assert "real prose here" in out and "Title" in out
    assert "evil()" not in out and "color:red" not in out


def test_refresh_and_reference(conn):
    src = FakeBlogSource([
        BlogPost("blog/posts/a.html", "I shipped a thing. Value must be obvious in three minutes."),
        BlogPost("blog/posts/b.html", "Another candid post about failing and learning."),
    ])
    llm = FakeVoiceLLM()
    assert voice.refresh_voice(conn, src, llm, "claude-haiku-4-5", now=NOW) is True
    ref = voice.voice_reference(conn)
    assert "candid" in ref and "Excerpts" in ref
    assert "shipped a thing" in llm.calls[0]["user"]  # corpus reached the model


def test_offline_refresh_has_excerpts_no_guide(conn):
    src = FakeBlogSource([BlogPost("a", "some genuine writing in my voice")])
    assert voice.refresh_voice(conn, src, None, "m", now=NOW) is True
    ref = voice.voice_reference(conn)
    assert "Excerpts" in ref and "genuine writing" in ref


def test_empty_source_no_update(conn):
    assert voice.refresh_voice(conn, FakeBlogSource([]), None, "m", now=NOW) is False
    assert voice.voice_reference(conn) == ""


def test_staleness(conn):
    assert voice.voice_is_stale(conn, now=NOW, max_age_days=7) is True  # nothing cached
    voice.refresh_voice(conn, FakeBlogSource([BlogPost("a", "writing")]), None, "m", now=NOW)
    assert voice.voice_is_stale(conn, now=NOW, max_age_days=7) is False
    assert voice.voice_is_stale(conn, now=NOW + timedelta(days=8), max_age_days=7) is True


def test_fetch_failure_is_safe(conn):
    class Boom:
        def fetch_posts(self):
            raise RuntimeError("network down")

    assert voice.refresh_voice(conn, Boom(), None, "m", now=NOW) is False  # no crash
