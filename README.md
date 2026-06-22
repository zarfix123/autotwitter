# autotwitter — X Growth Engine

A compliance-first personal X (Twitter) growth tool for building in public. It
**fully automates original posting** (the only thing X's 2026 rules allow to be
automated) and keeps a **real human in the loop** for every engagement action.

> **Status:** Phase 1 (zero-touch posting core) is implemented and tested.
> Phases 2–4 (Telegram approval + engagement gate, analytics/feedback, hardening)
> are planned — see `Roadmap` below.

## The one rule that shapes everything

X bans accounts on *behavioral patterns*, regardless of who clicked a button. So
this tool **never** auto-likes/follows/replies/reposts/DMs. That isn't a flag — it
is structural:

- The only X write surface in Phase 1 (`x_client.XPoster`) can post original
  tweets and reply **to our own** tweet (for the link-in-first-reply). It has no
  ability to touch other accounts.
- Engagement (replying to others, following) will arrive in Phase 2 behind an
  **engagement gate** that requires a fresh, single-use, per-item human approval
  token minted only by a real Telegram tap. No "agent approves the queue" mode.

Other guardrails already in place: secrets are scrubbed out of repo content before
anything reaches Claude or a draft; links go in the first reply (never the body);
posting is capped, spaced, and jittered; a kill switch pauses posting and clears
the queue; every action is audit-logged; API spend is tracked against a weekly cap.

## How Phase 1 works

```
GitHub commits ─poll→ Git Watcher ─scrub+classify(Haiku)→ git_event (deduped)
   → Content Generator (Sonnet, body URL-free + link as separate field)
   → Scheduler (≤2/day, ≥3h apart, jittered, in your windows)
   → Poster (body tweet first, then link as a reply to our own tweet)
```

It polls each watched repo's commits (no tags/releases/PRs needed), clusters new
commits, asks the cheap model whether they're a meaningful build-in-public moment,
drafts a post in your voice, schedules it into your active windows, and posts it.

## Setup

Requires Python 3.11+.

```bash
pip install -r requirements.txt          # runtime
pip install -r requirements-dev.txt      # + tests/lint

cp .env.example .env                      # fill in secrets
cp config.example.yaml config.yaml        # edit repos, voice, windows, cadence
```

- `.env` holds secrets (Anthropic key, GitHub PAT, X OAuth 2.0 user tokens,
  Telegram). Never commit it. `XGROWTH_DRY_RUN=1` (the default) means no live
  tweets — the poster logs synthetic ids so you can watch the pipeline safely.
- `config.yaml` holds all behavior (watched repos, topic clusters, voice samples,
  posting windows, `posts_per_day`, caps, cost cap, model choices).

## Run

```bash
# from the repo root
PYTHONPATH=src python -m xgrowth.app
```

This starts the FastAPI admin server and the APScheduler loops
(`watch_cycle` every 30 min, `post_tick` every minute).

Admin endpoints (default `http://127.0.0.1:8080`):

| Method | Path            | What it does                                  |
|--------|-----------------|-----------------------------------------------|
| GET    | `/health`       | liveness                                      |
| GET    | `/status`       | counts + weekly spend + paused flag           |
| POST   | `/admin/kill`   | **kill switch**: pause posting, clear queue   |
| POST   | `/admin/resume` | unpause                                        |

### Going live

1. Run with `XGROWTH_DRY_RUN=1` first and watch `/status` + the audit log fill in.
2. Provide X OAuth 2.0 **user-context** tokens (scope `tweet.write`) in `.env`.
   Never use password login or browser automation (both are bannable).
3. Set `XGROWTH_DRY_RUN=0` to post for real.

### Deploy (any cheap always-on instance, e.g. AWS Lightsail/EC2)

Polling needs no inbound port. Run the process under systemd or Docker with the
`.env` and `config.yaml` mounted, and a persistent volume for `data/xgrowth.db`.

## Develop

```bash
PYTHONPATH=src python -m pytest -q     # 48 tests, no network/keys needed
python -m ruff check .                  # lint
```

The LLM, GitHub source, and X poster are all injectable, so the whole pipeline
runs offline in tests via fakes.

## Roadmap

- **Phase 1 — done:** git watcher, secret scrubber, content generator, scheduler,
  poster, audit log, cost tracker, kill switch.
- **Phase 2:** read-only monitor + reply drafter + Telegram approval bot + the
  engagement gate (per-item human approval tokens) + paced follow candidates.
- **Phase 3:** analytics feedback loop + live-reply notifier.
- **Phase 4:** hardening — static no-auto-engagement assertions, observability,
  cost dashboard, docs.

See `/root/.claude/plans/x-growth-engine-structured-waffle.md` for the full plan.
