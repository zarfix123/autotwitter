"""Secret scrubber.

Hard requirement: this tool reads private repos and turns them into public posts.
No secret may ever reach the content generator or a draft. Every piece of repo
content (commit messages, file paths, diffs) passes through `scrub_text` first.

Strategy: high-precision regex for known credential shapes + a Shannon-entropy
fallback for long random-looking tokens. Matches are replaced with
``[REDACTED:<kind>]`` and reported, so callers can also choose to drop content
entirely (the git watcher drops any diff hunk that contained a secret).
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

# Ordered (kind, compiled pattern). High-precision shapes first.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("private_key_block", re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----.*?"
        r"-----END (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----",
        re.DOTALL,
    )),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")),
    ("openai_key", re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9]{20,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("github_pat_fine", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("aws_access_key", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
    ("slack_token", re.compile(r"\bxox[baprs]-[0-9A-Za-z\-]{10,}")),
    ("stripe_key", re.compile(r"\b[rs]k_(?:live|test)_[0-9A-Za-z]{16,}\b")),
    ("jwt", re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")),
    ("connection_string", re.compile(
        r"\b[a-z][a-z0-9+.\-]*://[^\s:@/]+:[^\s:@/]+@[^\s/]+",  # scheme://user:pass@host
    )),
    ("private_ip", re.compile(
        r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    )),
    ("internal_host", re.compile(r"\b[A-Za-z0-9.\-]+\.(?:internal|local|corp|intranet)\b")),
]

# Assignments whose KEY name implies a secret, e.g. .env lines: API_SECRET=abc123
_SECRET_ASSIGNMENT = re.compile(
    r"(?im)^\s*(?:export\s+)?"
    r"([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API[_-]?KEY|ACCESS[_-]?KEY|"
    r"PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|AUTH|CREDENTIAL)[A-Z0-9_]*)"
    r"\s*[:=]\s*['\"]?([^\s'\"#]+)['\"]?"
)

# Entropy fallback: long token-like substrings.
_TOKEN_CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-]{24,}")
_ENTROPY_THRESHOLD = 4.0  # bits/char; random base64/hex sits ~4.5-6, English ~2.5-3.5


@dataclass
class ScrubResult:
    text: str
    redactions: list[str]  # kinds that were redacted

    @property
    def had_secret(self) -> bool:
        return len(self.redactions) > 0


def shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


def scrub_text(text: str) -> ScrubResult:
    """Redact secrets from arbitrary text. Returns scrubbed text + what was hit."""
    if not text:
        return ScrubResult(text="", redactions=[])

    redactions: list[str] = []
    out = text

    # 1) Known credential shapes.
    for kind, pat in _PATTERNS:
        def _repl(_m: re.Match[str], _kind: str = kind) -> str:
            redactions.append(_kind)
            return f"[REDACTED:{_kind}]"

        out = pat.sub(_repl, out)

    # 2) Secret-looking assignments — redact the value, keep the key name.
    def _assign_repl(m: re.Match[str]) -> str:
        redactions.append("secret_assignment")
        return f"{m.group(1)}=[REDACTED:secret_assignment]"

    out = _SECRET_ASSIGNMENT.sub(_assign_repl, out)

    # 3) Entropy fallback for anything random-looking that slipped through.
    def _entropy_repl(m: re.Match[str]) -> str:
        tok = m.group(0)
        if tok.startswith("[REDACTED:"):
            return tok
        if shannon_entropy(tok) >= _ENTROPY_THRESHOLD:
            redactions.append("high_entropy")
            return "[REDACTED:high_entropy]"
        return tok

    out = _TOKEN_CANDIDATE.sub(_entropy_repl, out)

    return ScrubResult(text=out, redactions=redactions)


def scrub_diff(diff: str) -> ScrubResult:
    """Scrub a unified diff. Whole hunks/lines are kept but secrets within are
    redacted; ``had_secret`` lets the caller drop the content entirely if desired.
    """
    return scrub_text(diff)
