"""extract_json_field: robust body/reply extraction that never leaks JSON scaffolding."""

from __future__ import annotations

from xgrowth.textfmt import extract_json_field


def test_clean_json():
    assert extract_json_field('{"body": "hello world"}', "body") == "hello world"


def test_truncated_json_does_not_leak_wrapper():
    # A post that ran past max_tokens: invalid JSON, no closing quote or brace.
    text = '{"body": "shipped something big today and then it ran'
    out = extract_json_field(text, "body")
    assert out == "shipped something big today and then it ran"
    assert "{" not in out and "body" not in out  # the {"body": wrapper never survives


def test_clean_json_unescapes_newlines():
    assert extract_json_field('{"body": "line one\\n\\nline two"}', "body") == "line one\n\nline two"


def test_works_for_other_keys():
    assert extract_json_field('{"reply": "nice point about evals"}', "reply") == "nice point about evals"


def test_no_json_at_all_returns_stripped_text():
    assert extract_json_field("just plain text, no json here", "body") == "just plain text, no json here"


def test_markdown_fenced_json():
    assert extract_json_field('```json\n{"body": "fenced"}\n```', "body") == "fenced"
