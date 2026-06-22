"""Secret scrubber: planted secrets must all be redacted; benign text preserved.

Note: the credential-shaped fixtures are assembled at runtime from fragments
(``_join``) so the source file contains no contiguous secret literal — that keeps
platform secret-scanning happy while still exercising the scrubber's patterns.
"""

from __future__ import annotations

import pytest

from xgrowth import scrub


def _join(*parts: str) -> str:
    return "".join(parts)


# (kind, assembled-fake-secret) — none of these are real or valid.
CASES = [
    ("anthropic_key", _join("sk-ant-", "api03-", "AbCdEf0123456789AbCdEf0123456789xx")),
    ("openai_key", _join("sk-", "proj-", "AbCdEf0123456789AbCdEf0123456789")),
    ("github_token", _join("ghp", "_", "AbCdEf0123456789AbCdEf0123456789abcd")),
    ("aws_access_key", _join("AKIA", "ABCDEFGHIJ123456")),
    ("google_api_key", _join("AIza", "SyA1234567890abcdefghijklmnopqrstuvw")),
    ("slack_token", _join("xoxb", "-", "1234567890-abcdefghijklmnop")),
    ("stripe_key", _join("sk", "_live_", "0123456789abcdefghijklmn")),
]


@pytest.mark.parametrize("kind,secret", CASES)
def test_known_secrets_redacted(kind, secret):
    result = scrub.scrub_text(f"value here: {secret} end")
    assert result.had_secret, f"{kind} not detected in {secret!r}"
    assert secret not in result.text
    assert "[REDACTED:" in result.text


def test_private_key_block_redacted():
    # Markers assembled at runtime to avoid a literal key block in source.
    begin = _join("-----", "BEGIN RSA PRIVATE KEY", "-----")
    end = _join("-----", "END RSA PRIVATE KEY", "-----")
    body = "MIIEowIBAAKCAQEA" + "abc123def456"
    text = f"{begin}\n{body}\n{end}"
    result = scrub.scrub_text(text)
    assert "private_key_block" in result.redactions
    assert body not in result.text


def test_connection_string_redacted():
    result = scrub.scrub_text("db postgres://admin:s3cretpw@db.example.com:5432/app")
    assert result.had_secret
    assert "s3cretpw" not in result.text


def test_private_ip_and_internal_host_redacted():
    result = scrub.scrub_text("host 10.0.3.14 see api.corp.internal for details")
    assert "private_ip" in result.redactions
    assert "internal_host" in result.redactions


def test_env_assignment_value_redacted_key_kept():
    result = scrub.scrub_text("API_SECRET=hunter2supersecretvalue")
    assert result.had_secret
    assert "API_SECRET=" in result.text
    assert "hunter2supersecretvalue" not in result.text


def test_high_entropy_token_redacted():
    # A random 40-char token with no known prefix should trip the entropy fallback.
    result = scrub.scrub_text("blob 9f8A3kZ1qW7eR2tY6uI0oP4sD8fG5hJ2kL9mN3bV done")
    assert "high_entropy" in result.redactions


def test_benign_text_preserved():
    text = "shipped the reply ranking engine and wired up scheduler caps today"
    result = scrub.scrub_text(text)
    assert not result.had_secret
    assert result.text == text


def test_normal_words_not_flagged_as_entropy():
    text = "implementation configuration documentation refactoring"
    result = scrub.scrub_text(text)
    assert not result.had_secret
