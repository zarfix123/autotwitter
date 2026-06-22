"""Shared text helpers for drafted content (posts and replies).

URL detection/stripping and length caps live here so both the original-post
generator and the reply drafter enforce the same rules without duplication.
"""

from __future__ import annotations

import re

URL_RE = re.compile(r"https?://\S+|\bwww\.\S+|\b[\w.\-]+\.(?:com|io|dev|ai|app|org|net|co)\b/\S*")

MAX_LEN_STANDARD = 280
MAX_LEN_PREMIUM = 4000


def contains_url(text: str) -> bool:
    return URL_RE.search(text) is not None


def strip_urls(text: str) -> str:
    return URL_RE.sub("", text).strip()


def max_len(x_premium: bool) -> int:
    return MAX_LEN_PREMIUM if x_premium else MAX_LEN_STANDARD


def clamp(text: str, x_premium: bool) -> str:
    limit = max_len(x_premium)
    return text[:limit].rstrip() if len(text) > limit else text
