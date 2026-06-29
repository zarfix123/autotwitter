# Setup Guide

End-to-end setup for **autotwitter**, from zero to a bot running 24/7. This is the
hands-on companion to the architecture overview in [README.md](README.md) and the
ops runbook in [DEPLOY.md](DEPLOY.md).

> **What it does, in one line:** fully automates your *original* X posts (about your
> commits and about trending AI news, in your voice) and routes every *engagement*
> (replies, follows) through one-tap human approval on Telegram. See the README for
> the why.

There's some self-led work here (creating accounts, choosing what to watch). Where a
value is your choice, this guide says so and gives a sensible suggestion.

---

## 0. Prerequisites

- **Python 3.10+** (3.11+ works too) for local runs, **or** Docker for deployment.
- A **GitHub** account (your commits are one content source).
- ~30 minutes for the credential setup below.

```bash
git clone https://github.com/<you>/autotwitter.git
cd autotwitter
cp .env.example .env             # secrets (gitignored — never commit)
cp config.example.yaml config.yaml   # behavior (gitignored)
```

You'll fill in `.env` and `config.yaml` as you go.

---

## 1. Get your credentials

You don't need all of these to start — the bot runs in a safe **dry-run** with just
an Anthropic key. Add the rest as you turn features on.

### 1a. Anthropic API key (recommended — drives drafting)
1. Go to **console.anthropic.com → API keys → Create key**.
2. Add a few dollars of credit (pay-as-you-go).
3. Put it in `.env`: `ANTHROPIC_API_KEY=sk-ant-...`

Without it, drafting falls back to a crude heuristic (no real writing, no web
search) — so this is effectively required for good output. Cost is small (cents/day
at the default cadence).

### 1b. GitHub (commit polling)
- **Public repos:** nothing needed — polling works unauthenticated (just rate-limited).
- **Private repos:** create a fine-grained PAT with **read-only Contents** access to
  the repos you watch → `.env`: `GITHUB_TOKEN=github_pat_...`

### 1c. X / Twitter (required to post or read)
X is **pay-per-use** (as of 2026): you buy prepaid credits and pay per request
(~$0.015/post, $0.005/read, etc.). There's no free tier.

1. **developer.x.com** → sign up for a developer account → create a **Project + App**.
2. App → **User authentication settings** → **Set up**:
   - **App permissions:** Read and write
   - **Type of App:** *Web App, Automated App or Bot* (a confidential client — gives
     you a client secret, which the auto-refresh needs)
   - **Callback URI / Redirect URL:** `https://127.0.0.1/callback` (must match exactly)
   - **Website URL:** any valid https URL (e.g. your GitHub) — `localhost` is rejected here
3. App → **Keys and tokens** → copy the **OAuth 2.0 Client ID and Client Secret**
   (the *OAuth 2.0* pair, **not** the API Key/Secret or Bearer token):
   ```
   X_CLIENT_ID=...
   X_CLIENT_SECRET=...
   ```
4. **Load credits** in the developer console (a few dollars) — posts are rejected at $0.
5. The **user access/refresh tokens** are minted in [step 4](#4-mint-your-x-tokens).

> The repo posts the link as a *self-reply* (never in the body) — that keeps the main
> tweet at the cheap rate and avoids X down-ranking link posts.

### 1d. Telegram (optional — the engagement approval/notification layer)
Only needed if you want reply/follow suggestions pushed to your phone. Posting works
without it.
1. Install **Telegram**, make an account.
2. In Telegram, message **@BotFather** → `/newbot` → pick a name + username. It returns
   a **token** → `.env`: `TELEGRAM_BOT_TOKEN=...`
3. Message **@userinfobot** → it replies with your numeric id → `.env`:
   `TELEGRAM_ALLOWED_USER_ID=...` (only this user can approve/command the bot).
4. **Open your new bot and tap Start** — Telegram won't let a bot DM you until you do.

### 1e. Blog voice (optional — makes posts sound like you)
If you have a blog, the bot can distill your writing style from it. GitHub-hosted
blogs (GitHub Pages) work best — set in `config.yaml`:
```yaml
voice_blog_repo: <you>/<you>.github.io   # the repo holding your blog
voice_blog_path: blog/posts               # folder with the post files (.html/.md)
```
No blog? Leave `voice_blog_repo` empty and the bot uses the `voice_samples` you write
in `config.yaml` instead.

---

## 2. Configure behavior (`config.yaml`)

Everything in `config.example.yaml` is annotated — that file is the source of truth.
The fields you'll most likely edit:

| Field | What it is | Suggestion |
|---|---|---|
| `repos`, `github_author` | repos to watch + your GitHub login | your project repos |
| `topic_clusters` | your niche (drives relevance + framing) | e.g. `AI`, `edtech` |
| `voice_samples` | 2-5 real posts so drafts sound like you | paste your best tweets |
| `posting_windows` | when to post (LOCAL time, `HH:MM-HH:MM`) | your audience's active hours |
| `posts_per_day` | hard daily cap across all post types | `2` (research sweet spot 2-4) |
| `commit_posts_per_day` / `ai_news_max_per_day` | the daily mix split | `1` + `1` = ~1 work + 1 world |
| `commit_window_days` | "recent" commits to choose the best from | `7` |
| `ai_news_enabled` | post about trending AI (Hacker News + web search) | `true` once you're set |
| `target_accounts`, `keywords` | who/what to mine for replies (engagement) | leave empty until you want it |
| `weekly_cost_cap_usd` | hard weekly spend ceiling (auto-throttles) | `15` |
| `models` | per-job model IDs | the defaults are fine |

> **Engagement (replies/follows) is OFF when `target_accounts` and `keywords` are
> empty** *and* Telegram isn't configured. Turn it on only when you're ready to pay
> for X reads and tap a daily batch.

And `.env` runtime knobs (see `.env.example` for all): `TZ` (your timezone — **set
this on a server**), `XGROWTH_DRY_RUN` (`1` = safe default, nothing posts).

---

## 3. Run it locally (dry run first)

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=src .venv/bin/python -m xgrowth.app
```

With `XGROWTH_DRY_RUN=1` (default) nothing is posted — the poster logs synthetic ids
so you can watch the pipeline safely. You should see `Scheduler started.` and, if
Telegram is configured, `Telegram bot polling.` Message your bot `/status` to confirm
the round-trip.

Run the tests anytime (offline, no keys needed):
```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
.venv/bin/python -m ruff check .
```

---

## 4. Mint your X tokens

This is the one OAuth dance. A zero-dependency helper does it:

```bash
python3 scripts/x_oauth.py
```

It reads your `X_CLIENT_ID`/`SECRET` from `.env`, prints an authorization URL → open
it, click **Authorize**, copy the `https://127.0.0.1/callback?...` URL you're
redirected to (the page won't load — that's fine), and paste it back into the prompt.
It writes `X_ACCESS_TOKEN` + `X_REFRESH_TOKEN` into `.env`. From then on the bot
**auto-refreshes** the token forever.

> ⚠️ **X authorization codes expire in ~30 seconds.** Run this script **on the same
> machine that holds the `.env`** (e.g. directly on your server via its own SSH), and
> paste the redirect URL *immediately*. Relaying the code through a chat/another hop
> will usually fail with "authorization code was invalid" — that's expiry, not a bug.
> Keep `offline.access` in the scopes (it's the default) or you won't get a refresh
> token.

---

## 5. Go live

1. Confirm the dry run looks right (`/status`, logs, no tracebacks).
2. Set `XGROWTH_DRY_RUN=0` in `.env`.
3. Restart. Real posting, reads, and approved engagement are now active — all bounded
   by `weekly_cost_cap_usd`.

---

## 6. Deploy on a cheap always-on box

The bot is a single long-lived process needing **outbound network only** (no inbound
ports). Run it on any small Linux VM so it's not tied to your laptop.

**Good cheap options:** AWS Lightsail (~$5/mo, simple), Oracle Cloud Always Free
($0), or Hetzner (~€4/mo). **Use at least 1 GB RAM** — 512 MB can't build the image.

Quickest path (Docker), on the VM:
```bash
curl -fsSL https://get.docker.com | sudo sh        # install Docker
git clone https://github.com/<you>/autotwitter.git && cd autotwitter
# copy your .env + config.yaml onto the box (scp from your laptop), then:
mkdir -p data && sudo chown -R 10001 data          # container runs as uid 10001
docker compose up -d --build
docker compose logs -f
```

Full runbook (Docker + systemd, dry-run smoke test, updates, backups) is in
**[DEPLOY.md](DEPLOY.md)**. Mint your X tokens **on the box** (step 4) to dodge the
code-expiry issue. Set `TZ` in `.env` to your timezone so posting windows fire right.

---

## 7. Operating it

- **Pause/resume:** Telegram `/kill` (pauses posting + clears the queue) and `/resume`.
  Also `POST /admin/kill` / `/admin/resume`.
- **Status:** Telegram `/status` or `GET /status` — counts + weekly spend + paused flag.
- **Approvals:** the bot DMs a daily batch (and live pings) of drafted replies/follows
  with Approve/Skip buttons; `/now` pulls the batch on demand.
- **Spend:** `weekly_cost_cap_usd` pauses non-essential reads + AI-news drafting as
  it's approached; posting and already-approved replies are never blocked.
- **Update:** `git pull && docker compose up -d --build`.
- **Backups:** back up `data/xgrowth.db` (dedup cursors, history, cost, voice cache,
  approval tokens).

---

## 8. Troubleshooting (real gotchas)

| Symptom | Cause & fix |
|---|---|
| Posts fire at the wrong time | Cloud VMs default to **UTC**; `posting_windows` are local. Set `TZ=America/Los_Angeles` (your zone) in `.env`. |
| `sqlite3.OperationalError: unable to open database file` (Docker) | The container runs as uid `10001` but `data/` is owned by your user. Run `sudo chown -R 10001 data` and restart. |
| Build dies with `signal: killed` / OOM | Instance too small. Use **≥1 GB RAM**, or add swap: `sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile` (persist via `/etc/fstab`). |
| X mint: `authorization code was invalid` | The code **expired (~30s)**. Run `scripts/x_oauth.py` on the machine with the `.env` and paste the redirect URL immediately — don't relay it through another hop. |
| Telegram: `InvalidToken ... rejected by the server` | A copy error (`0`↔`O`, `l`↔`1`) or a revoked token. Re-copy from BotFather (`/token`). **Note:** a bad/unreachable Telegram token currently crash-loops the whole process — to run *without* the bot, blank `TELEGRAM_BOT_TOKEN`. |
| Nothing posts after going live | Normal — posts only fire inside `posting_windows`, and a commit post needs a meaningful commit within `commit_window_days`. Check `/status` and the audit log. Posting also needs `X_ACCESS_TOKEN` set + `XGROWTH_DRY_RUN=0`. |
| `cannot import name 'UTC' from 'datetime'` | You're on Python <3.10. Use 3.10+. |

---

## 9. Costs & security

- **Costs:** VM ($0–5/mo) + Anthropic (cents–few $/mo) + X pay-per-use (bounded by
  your weekly cap). Realistically well under $30/mo all-in; ~$0 VM on Oracle's free tier.
- **Secrets** live only in `.env` (gitignored). Never commit them. If a key is ever
  exposed, **rotate it** — for X, rotate the client secret *before* minting tokens
  (rotating it later breaks refresh and forces a re-mint).
- The **admin HTTP API has no auth** — keep `XGROWTH_HOST=127.0.0.1` (the default) and
  never expose port 8080 publicly.
- **Compliance:** the tool never auto-engages — replies/follows require a per-item
  human tap, enforced structurally and checked by `tests/test_guardrail.py`. Keep it
  that way; X bans on automated engagement patterns.
