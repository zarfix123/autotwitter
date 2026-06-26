#!/usr/bin/env python3
"""One-time X (Twitter) OAuth 2.0 token minting (Authorization Code + PKCE).

Zero dependencies (Python standard library only) — run it in a normal terminal
with network access. It reads X_CLIENT_ID / X_CLIENT_SECRET from .env, walks you
through the browser authorization, then writes X_ACCESS_TOKEN / X_REFRESH_TOKEN
back into .env. The running app auto-refreshes from there (see src/xgrowth/x_auth.py).

Prerequisites (X developer portal, developer.x.com):
  1. A Project + App with **User authentication settings** turned on.
  2. App permissions: **Read and write**.
  3. Type: "Web App, Automated App or Bot" (confidential client -> has a client secret).
  4. A **Callback / Redirect URI** matching X_REDIRECT_URI below (default
     https://127.0.0.1/callback). The page won't load after you authorize — that's
     expected; you just copy the address-bar URL.

Usage:
  python3 scripts/x_oauth.py            # reads X_CLIENT_ID/SECRET from .env
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request

AUTH_URL = "https://x.com/i/oauth2/authorize"
TOKEN_URL = "https://api.twitter.com/2/oauth2/token"
SCOPES = "tweet.read tweet.write users.read offline.access"
ENV_PATH = os.environ.get("ENV_PATH", ".env")
DEFAULT_REDIRECT = "https://127.0.0.1/callback"


def read_env(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    if not os.path.exists(path):
        return out
    for line in open(path):
        m = re.match(r"^(\w+)=(.*)$", line.rstrip("\n"))
        if m:
            out[m.group(1)] = m.group(2)
    return out


def upsert_env(path: str, updates: dict[str, str]) -> None:
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    seen = set()
    for i, ln in enumerate(lines):
        m = re.match(r"^(\w+)=", ln)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}"
            seen.add(m.group(1))
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}")
    open(path, "w").write("\n".join(lines) + "\n")


def main() -> int:
    env = read_env(ENV_PATH)
    client_id = os.environ.get("X_CLIENT_ID") or env.get("X_CLIENT_ID", "")
    client_secret = os.environ.get("X_CLIENT_SECRET") or env.get("X_CLIENT_SECRET", "")
    redirect_uri = os.environ.get("X_REDIRECT_URI") or env.get("X_REDIRECT_URI") or DEFAULT_REDIRECT

    if not client_id or not client_secret:
        print(f"error: set X_CLIENT_ID and X_CLIENT_SECRET in {ENV_PATH} first "
              "(the OAuth 2.0 Client ID + Secret from your X app).", file=sys.stderr)
        return 2

    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    auth = AUTH_URL + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })

    print("\n1) Open this URL in your browser (signed into the X account to post from):\n")
    print("   " + auth + "\n")
    print("2) Click 'Authorize app'.")
    print(f"3) Your browser redirects to {redirect_uri} — the page won't load, that's fine.")
    print("   Copy the FULL address-bar URL (it has ?state=...&code=...).\n")

    response_url = input("Paste the redirected URL here: ").strip()
    qs = urllib.parse.parse_qs(urllib.parse.urlparse(response_url).query)
    code = (qs.get("code") or [""])[0]
    if not code:
        print("error: no ?code= found in that URL. Paste the full redirected URL.", file=sys.stderr)
        return 2
    if (qs.get("state") or [""])[0] != state:
        print("error: state mismatch — run the script again from scratch.", file=sys.stderr)
        return 2

    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": verifier,
        "client_id": client_id,
    }).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL, data=body,
        headers={"Authorization": f"Basic {basic}",
                 "Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        payload = json.load(urllib.request.urlopen(req, timeout=30))
    except urllib.error.HTTPError as e:
        print(f"token exchange failed ({e.code}): {e.read().decode()[:300]}", file=sys.stderr)
        print("(if it says the code expired, just re-run — codes are only valid briefly.)",
              file=sys.stderr)
        return 1

    access = payload.get("access_token")
    refresh = payload.get("refresh_token", "")
    if not access:
        print(f"error: no access_token in response: {payload}", file=sys.stderr)
        return 1

    upsert_env(ENV_PATH, {
        "X_ACCESS_TOKEN": access,
        "X_REFRESH_TOKEN": refresh,
        "X_REDIRECT_URI": redirect_uri,
    })
    print(f"\n✅ Wrote X_ACCESS_TOKEN + X_REFRESH_TOKEN to {ENV_PATH}.")
    print(f"   scopes: {payload.get('scope', '?')}")
    print(f"   expires_in: {payload.get('expires_in', '?')}s | refresh token: "
          f"{'present (auto-refresh enabled)' if refresh else 'MISSING — add offline.access'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
