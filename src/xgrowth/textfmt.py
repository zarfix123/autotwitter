"""Shared text helpers for drafted content (posts and replies).

URL detection/stripping and length caps live here so both the original-post
generator and the reply drafter enforce the same rules without duplication.
"""

from __future__ import annotations

import json
import re

URL_RE = re.compile(r"https?://\S+|\bwww\.\S+|\b[\w.\-]+\.(?:com|io|dev|ai|app|org|net|co)\b/\S*")


def extract_json_field(text: str, key: str) -> str:
    """Pull a string field from a model's JSON-ish reply.

    Robust to truncation (a post that ran past max_tokens leaves invalid JSON) and to
    markdown fences — it never returns the ``{"key": "..."}`` scaffolding itself, which
    would otherwise leak into a posted tweet.
    """
    # 1) Clean JSON object.
    try:
        blob = text[text.index("{") : text.rindex("}") + 1]
        value = json.loads(blob).get(key, "")
        if value:
            return str(value).strip()
    except (ValueError, json.JSONDecodeError):
        pass
    # 2) Regex the value out (handles truncated / unterminated JSON).
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*"(.*)', text, re.DOTALL)
    if m:
        value = m.group(1)
        end = re.search(r'(?<!\\)"', value)  # stop at the closing quote if there is one
        value = value[: end.start()] if end else value  # else it was truncated — take it all
        return value.replace("\\n", "\n").replace('\\"', '"').replace("\\t", "\t").strip()
    # 3) No structure at all — strip stray braces/quotes so the wrapper never survives.
    return text.strip().strip("{}").strip().strip('"').strip()

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
