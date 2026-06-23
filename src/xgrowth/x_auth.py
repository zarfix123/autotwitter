"""OAuth 2.0 user-token lifecycle for X: persistence + automatic refresh.

X OAuth 2.0 user-context access tokens expire (~2h) and refresh tokens *rotate* on
every use. The write/read/engage surfaces are long-lived (one process, cron jobs
over hours/days), so a raw access token from ``.env`` would start failing after the
first window. This module keeps a valid token available:

  * the current token set is persisted in the ``settings`` table (survives restarts),
  * ``XTokenProvider.token()`` refreshes it from the token endpoint when it's within
    a skew window of expiry, persisting the rotated tokens,
  * a refresh failure degrades gracefully (returns the last token, audits the error)
    rather than crashing the loop.

``.env`` only seeds the *first* run; after that the DB is the source of truth. To
re-seed (e.g. after re-minting via ``scripts/x_oauth.py``), clear the ``x_access_token``
/ ``x_refresh_token`` / ``x_token_expires_at`` settings rows.
"""

from __future__ import annotations

import base64
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from . import audit, db

TOKEN_ENDPOINT = "https://api.twitter.com/2/oauth2/token"

_K_ACCESS = "x_access_token"
_K_REFRESH = "x_refresh_token"
_K_EXPIRES = "x_token_expires_at"

# (url, *, data, headers) -> parsed JSON dict. Injectable so tests never hit network.
PostFn = Callable[..., dict]


@dataclass
class TokenSet:
    access_token: str
    refresh_token: str | None
    expires_at: datetime  # UTC


def load_tokens(conn: sqlite3.Connection) -> TokenSet | None:
    access = db.get_setting(conn, _K_ACCESS)
    if not access:
        return None
    raw_exp = db.get_setting(conn, _K_EXPIRES)
    try:
        expires_at = datetime.fromisoformat(raw_exp) if raw_exp else datetime.now(UTC)
    except ValueError:
        expires_at = datetime.now(UTC)
    return TokenSet(access, db.get_setting(conn, _K_REFRESH), expires_at)


def save_tokens(conn: sqlite3.Connection, tokens: TokenSet) -> None:
    db.set_setting(conn, _K_ACCESS, tokens.access_token)
    if tokens.refresh_token:
        db.set_setting(conn, _K_REFRESH, tokens.refresh_token)
    db.set_setting(conn, _K_EXPIRES, tokens.expires_at.isoformat())


def _default_post(url: str, *, data: dict, headers: dict) -> dict:
    import requests  # lazy

    resp = requests.post(url, data=data, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


def refresh_tokens(
    client_id: str,
    client_secret: str | None,
    refresh_token: str,
    *,
    now: datetime | None = None,
    post_fn: PostFn | None = None,
) -> TokenSet:
    """Exchange a refresh token for a fresh token set at the X token endpoint.

    Confidential clients (a client secret is set) authenticate with HTTP Basic;
    public clients send ``client_id`` in the body. X rotates the refresh token, so
    the returned set carries the new one (falling back to the old if absent).
    """
    now = now or datetime.now(UTC)
    post_fn = post_fn or _default_post
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    if client_secret:
        basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        headers["Authorization"] = f"Basic {basic}"
    payload = post_fn(TOKEN_ENDPOINT, data=data, headers=headers)
    expires_in = int(payload.get("expires_in", 7200))
    return TokenSet(
        access_token=payload["access_token"],
        refresh_token=payload.get("refresh_token") or refresh_token,
        expires_at=now + timedelta(seconds=expires_in),
    )


class XTokenProvider:
    """Returns a currently-valid OAuth 2.0 user access token, refreshing as needed.

    ``token()`` is what the X surfaces call before each request. It opens a short-lived
    DB connection (via ``conn_factory``) so refreshed tokens are persisted immediately
    and shared across surfaces/restarts.
    """

    def __init__(
        self,
        *,
        client_id: str | None,
        client_secret: str | None,
        conn_factory: Callable[[], sqlite3.Connection],
        seed: TokenSet | None = None,
        skew_seconds: int = 120,
        now_fn: Callable[[], datetime] | None = None,
        post_fn: PostFn | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._conn_factory = conn_factory
        self._seed = seed
        self._skew = timedelta(seconds=skew_seconds)
        self._now = now_fn or (lambda: datetime.now(UTC))
        self._post_fn = post_fn

    def token(self) -> str:
        conn = self._conn_factory()
        try:
            tokens = load_tokens(conn)
            if tokens is None:
                if self._seed is None:
                    raise RuntimeError("no X token available (set X_ACCESS_TOKEN or run scripts/x_oauth.py)")
                tokens = self._seed
                save_tokens(conn, tokens)

            now = self._now()
            needs_refresh = now >= tokens.expires_at - self._skew
            if needs_refresh and tokens.refresh_token and self._client_id:
                try:
                    tokens = refresh_tokens(
                        self._client_id,
                        self._client_secret,
                        tokens.refresh_token,
                        now=now,
                        post_fn=self._post_fn,
                    )
                    save_tokens(conn, tokens)
                    audit.log(
                        conn, "x_token.refreshed",
                        detail={"expires_at": tokens.expires_at.isoformat()},
                    )
                except Exception as exc:  # noqa: BLE001 — never crash the loop on refresh failure
                    audit.log(conn, "x_token.refresh_failed", detail={"error": str(exc)})
            return tokens.access_token
        finally:
            conn.close()


def build_seed(
    access_token: str | None,
    refresh_token: str | None,
    *,
    now: datetime | None = None,
) -> TokenSet | None:
    """Build the first-run seed from ``.env``.

    With a refresh token, mark it already-expired so the first ``token()`` call
    refreshes immediately and gets a real, known expiry. Without one, treat the
    access token as long-lived (it can't be refreshed — degraded mode).
    """
    if not access_token:
        return None
    now = now or datetime.now(UTC)
    expires_at = now if refresh_token else now + timedelta(days=3650)
    return TokenSet(access_token, refresh_token, expires_at)
