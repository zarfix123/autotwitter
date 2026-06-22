# Deploying the X Growth Engine

This runs as a single always-on process (Telegram bot + scheduler + admin API) on
any small Linux instance — e.g. an AWS Lightsail/EC2 `t4g.small` or a cheap VPS. It
needs **outbound** network only (GitHub/X/Claude polling + Telegram long-poll), so no
inbound ports are required. The only state is one SQLite file.

Pick one path below: **Docker** (recommended) or **systemd**.

---

## 0. Prerequisites (both paths)

1. Clone the repo onto the instance and `cd` into it.
2. Create your config and secrets from the templates:
   ```bash
   cp .env.example .env
   cp config.example.yaml config.yaml
   ```
3. Edit **`config.yaml`** — watched repos, topic clusters, voice samples, posting
   windows, cadence, target accounts, caps. (See comments in the file.)
4. Edit **`.env`** — fill in the credentials you have. For the very first run, leave
   `XGROWTH_DRY_RUN=1` (the default): nothing is posted, read, or sent live.
   - `ANTHROPIC_API_KEY` — drafting/classification (optional for a dry smoke test; a
     heuristic fallback runs without it).
   - `GITHUB_TOKEN` — PAT with read access to the watched repos.
   - `X_*` — OAuth 2.0 user-context tokens (scope `tweet.write`, `follows.write`).
   - `TELEGRAM_BOT_TOKEN` + `TELEGRAM_ALLOWED_USER_ID` — from @BotFather and your
     numeric Telegram id (only that user can approve/command the bot).

> Secrets live only in `.env` (gitignored) and are never baked into the image.

---

## 1. Docker (recommended)

```bash
mkdir -p data                 # persisted SQLite volume
docker compose build
docker compose up -d
docker compose logs -f        # watch it start
```

You should see `Scheduler started.` and (if Telegram is configured)
`Telegram bot polling.`

- **Update:** `git pull && docker compose build && docker compose up -d`
- **Stop:** `docker compose down` (the `data/` volume persists)
- **Permissions:** the container runs as uid `10001`; if you hit a write error on
  `data/`, run `sudo chown -R 10001 data`.

---

## 2. systemd (no Docker)

```bash
sudo useradd -r -s /usr/sbin/nologin xgrowth || true
sudo mkdir -p /opt/autotwitter && sudo cp -r . /opt/autotwitter && cd /opt/autotwitter
sudo python3 -m venv .venv
sudo .venv/bin/pip install .
sudo chown -R xgrowth /opt/autotwitter

sudo cp deploy/xgrowth.service /etc/systemd/system/xgrowth.service
sudo systemctl daemon-reload
sudo systemctl enable --now xgrowth
journalctl -u xgrowth -f      # watch it start
```

- **Update:** copy the new code into `/opt/autotwitter`, `sudo .venv/bin/pip install .`,
  `sudo systemctl restart xgrowth`.

---

## 3. First-run smoke test (dry run — do this before going live)

With `XGROWTH_DRY_RUN=1`:

1. Confirm the logs show `Scheduler started.` with no tracebacks.
2. Check health (admin binds to `127.0.0.1:8080` inside the process):
   - Docker: `docker compose exec xgrowth python -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8080/health').read())"`
   - systemd: `curl localhost:8080/health` → `{"ok": true, ...}`
3. If Telegram is configured, message the bot `/status` and `/now`. In dry-run the
   engager is a no-op, so you can tap **Approve** on a drafted reply and confirm the
   full round-trip **without anything being sent live**.

When the dry run looks right, set `XGROWTH_DRY_RUN=0` in `.env` and restart
(`docker compose up -d` / `systemctl restart xgrowth`). Now real posting, reads, and
approved engagement are active.

---

## 4. Operating it

- **Kill switch:** Telegram `/kill` (or `POST /admin/kill`) pauses posting and clears
  the send queue; `/resume` (or `POST /admin/resume`) re-enables. `/status` shows
  counts + weekly spend.
- **Spend:** the weekly cost cap (`weekly_cost_cap_usd` in `config.yaml`) pauses the
  non-essential reads (monitor/analytics) as it's approached; posting and already
  approved replies are never blocked.
- **Backups:** back up `data/xgrowth.db` (dedup cursors, history, audit log, cost).
- **Admin API exposure:** keep it internal. Only expose port 8080 (and set
  `XGROWTH_HOST=0.0.0.0`) if you specifically need the HTTP admin from the host —
  it has no auth, so bind it to `127.0.0.1` and/or put it behind your firewall.
