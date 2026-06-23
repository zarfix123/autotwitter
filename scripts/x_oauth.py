#!/usr/bin/env python3
"""One-time X (Twitter) OAuth 2.0 token minting (Authorization Code + PKCE).

X user-context access tokens expire in ~2h; this mints the initial access +
*refresh* token pair so the running app can auto-refresh from there (see
src/xgrowth/x_auth.py). You run this once, locally, and paste the two values it
prints into your .env (X_ACCESS_TOKEN / X_REFRESH_TOKEN).

Prerequisites (X developer portal, developer.x.com):
  1. A Project + App with **User authentication settings** turned on.
  2. App permissions: **Read and write** (so tweet.write works).
  3. Type: "Web App, Automated App or Bot" (confidential client -> has a client secret).
  4. A **Callback / Redirect URI** — must match --redirect-uri below EXACTLY.
     A localhost URL is fine; the page won't load, you just copy the address bar.

Usage:
  # values can come from flags or the X_CLIENT_ID / X_CLIENT_SECRET env vars (.env)
  python scripts/x_oauth.py --redirect-uri "https://127.0.0.1/callback"

Then: open the printed URL, authorize, and paste the FULL URL you get redirected
to (it contains ?code=...&state=...) back into this script.
"""

from __future__ import annotations

import argparse
import os
import sys

DEFAULT_SCOPES = ["tweet.read", "tweet.write", "users.read", "offline.access"]


def _load_dotenv() -> None:
    try:
        from dotenv import load_dotenv  # optional convenience
    except ImportError:
        return
    load_dotenv()


def main() -> int:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Mint X OAuth 2.0 user tokens (one-time).")
    parser.add_argument("--client-id", default=os.environ.get("X_CLIENT_ID", ""))
    parser.add_argument("--client-secret", default=os.environ.get("X_CLIENT_SECRET", ""))
    parser.add_argument(
        "--redirect-uri",
        default=os.environ.get("X_REDIRECT_URI", "https://127.0.0.1/callback"),
        help="Must match a Callback URI configured on your X app exactly.",
    )
    parser.add_argument(
        "--scope", nargs="+", default=DEFAULT_SCOPES,
        help="OAuth scopes. Keep offline.access to receive a refresh token.",
    )
    args = parser.parse_args()

    if not args.client_id:
        print("error: set --client-id or X_CLIENT_ID (from your X app's OAuth 2.0 settings).",
              file=sys.stderr)
        return 2
    if "offline.access" not in args.scope:
        print("warning: without 'offline.access' you won't get a refresh token (no auto-refresh).",
              file=sys.stderr)

    try:
        import tweepy
    except ImportError:
        print("error: tweepy not installed. Run: pip install -r requirements.txt", file=sys.stderr)
        return 2

    handler = tweepy.OAuth2UserHandler(
        client_id=args.client_id,
        redirect_uri=args.redirect_uri,
        scope=list(args.scope),
        client_secret=args.client_secret or None,
    )

    auth_url = handler.get_authorization_url()
    print("\n1) Open this URL in your browser and authorize the app:\n")
    print("   " + auth_url + "\n")
    print(f"2) You'll be redirected to {args.redirect_uri} (the page may not load — that's fine).")
    print("   Copy the FULL address-bar URL (it has ?code=...&state=...) and paste it here.\n")

    response_url = input("Paste the redirected URL: ").strip()
    if not response_url:
        print("error: no URL provided.", file=sys.stderr)
        return 2

    token = handler.fetch_token(response_url)
    access = token.get("access_token")
    refresh = token.get("refresh_token")
    if not access:
        print(f"error: no access_token in response: {token}", file=sys.stderr)
        return 1

    print("\n--- Success. Paste these into your .env ---\n")
    print(f"X_ACCESS_TOKEN={access}")
    print(f"X_REFRESH_TOKEN={refresh or ''}")
    print(f"X_CLIENT_ID={args.client_id}")
    if args.client_secret:
        print(f"X_CLIENT_SECRET={args.client_secret}")
    print()
    if not refresh:
        print("note: no refresh token returned — add 'offline.access' to --scope and re-run "
              "for unattended operation.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
