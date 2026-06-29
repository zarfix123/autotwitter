"""Post de-duplication: word-overlap guard + recent-post lookup."""

from __future__ import annotations

from xgrowth import dedupe


def test_jaccard_identical_and_disjoint():
    assert dedupe.jaccard("the quick brown fox", "the quick brown fox") == 1.0
    assert dedupe.jaccard("alpha beta gamma", "delta epsilon zeta") == 0.0


def test_too_similar_catches_near_copy():
    recent = ["shipped the new scheduler today, feeling good about it"]
    assert dedupe.too_similar(
        "shipped the new scheduler today, feeling great about it", recent, threshold=0.6
    )
    assert not dedupe.too_similar(
        "completely unrelated thoughts about coffee beans", recent, threshold=0.6
    )


def test_too_similar_empty_recent_is_false():
    assert not dedupe.too_similar("anything at all here", [])


def test_recent_post_texts(conn):
    for b in ("post one body here", "post two body here"):
        conn.execute(
            "INSERT INTO drafts(kind, body, status, created_at) VALUES('post',?,'scheduled','t')",
            (b,),
        )
    conn.commit()
    texts = dedupe.recent_post_texts(conn)
    assert "post one body here" in texts and "post two body here" in texts
