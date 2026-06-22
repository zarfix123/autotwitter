# autotwitter — X Growth Engine

A compliance-first personal X (Twitter) growth tool for building in public. It
**fully automates original posting** (the only thing X's 2026 rules allow to be
automated) and keeps a **real human in the loop** for every engagement action.

> **Status:** Phases 1–3 implemented and tested (101 tests): zero-touch posting,
> the growth engine + Telegram approval + engagement gate, and the analytics
> feedback loop + live-reply notifier. Phase 4 (hardening) is planned — see
> `Roadmap` below.

## The one rule that shapes everything

X bans accounts on *behavioral patterns*, regardless of who clicked a button. So
this tool **never** auto-likes/follows/replies/reposts/DMs. That isn't a flag — it
is structural:

There are **three separate X surfaces**, and the separation *is* the safety model:

- `x_client.XPoster` (original posting) — original tweets + reply to **our own**
  tweet only. No other-account interaction.
- `x_read.XReader` (read-only) — search/timelines/user lookups. Cannot post or engage.
- `engagement.XEngager` (reply-to-others, follow) — reachable **only** through
  `engagement_gate`, which requires a fresh, single-use, item-bound approval token.
  Tokens are minted **only** by a real Telegram tap from the allow-listed user
  (`mint_approval_token`), enforced item-by-item. There is no "agent/script approves
  the queue" mode, and a static test (`tests/test_guardrail.py`) fails CI if any
  engagement endpoint, the gate, or token-minting leaks outside those modules.

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

## How Phase 2 works (the 5-minutes-a-day layer)

```
Monitor (read-only) ─rank(Haiku)→ reply_opportunities ─→ Reply Drafter (Sonnet)
   → Telegram daily push (Approve / Skip buttons)
   → Approve tap → mint token → engagement_gate → reply/follow sent (one action)
   → Skip tap   → discarded; nothing sent
```

The monitor reads target accounts + keyword searches, ranks by relevance ×
freshness × account size, and queues opportunities — it never acts. Claude drafts a
sharp, specific reply for each. Once a day (at a jittered time) the Telegram bot
sends you the batch; you approve or skip with one tap. Approving is the *only* path
that sends an engagement, and every approval is a fresh per-item token. Follows are
optional, low-capped (`max_follows_per_day`, `0` disables), and paced
(`follow_min_spacing_minutes`), enforced in the gate.

## How Phase 3 works (gets better + reacts in real time)

```
analytics_pull (owned reads) → snapshots → insights(top topics, best hours)
   → hint into the content generator   (drafts lean toward what lands)
   → preferred hours into the scheduler (timing leans toward what lands)

live_reply_tick → a target just posted (fresh)? → draft a reply now
   → one-tap Telegram push → [your tap] → engagement gate → sent
```

- **Analytics feedback loop** (`analytics.py`) — periodically pulls your own posts'
  metrics (cheap "owned" reads), stores a time series, and computes which topics and
  posting hours perform best. Those signals are fed *softly* into drafting and timing
  (only once there's enough data, so it doesn't over-fit). Deterministic, no LLM.
- **Live-reply notifier** (`live_reply.py`) — checks high-value target accounts every
  few minutes; when one posts something brand-new, it drafts a reply immediately and
  pushes a single one-tap approval, so a timely reply can land while the post is still
  climbing. It only reads and drafts — sending still goes through the same gate.

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

This runs the async loop: the Telegram approval bot (if configured), the scheduler
jobs (`watch_cycle`/30m, `post_tick`/1m, `monitor_scan`/configurable,
`reply_reminder`/daily-jittered, `analytics_pull`/configurable,
`live_reply_tick`/configurable), and the FastAPI admin server in a thread.

Admin endpoints (default `http://127.0.0.1:8080`):

| Method | Path            | What it does                                  |
|--------|-----------------|-----------------------------------------------|
| GET    | `/health`       | liveness                                      |
| GET    | `/status`       | counts + weekly spend + paused flag           |
| POST   | `/admin/kill`   | **kill switch**: pause posting, clear queue   |
| POST   | `/admin/resume` | unpause                                        |

### Telegram approval bot

1. Create a bot with [@BotFather](https://t.me/BotFather) → put the token in
   `TELEGRAM_BOT_TOKEN`. Message your bot once, then set `TELEGRAM_ALLOWED_USER_ID`
   to your numeric Telegram user id (only that user can approve or command the bot).
2. The bot DMs you a daily batch of drafted replies (and any follow candidates) with
   Approve/Skip buttons. Commands: `/status`, `/queue`, `/now` (push the batch on
   demand), `/kill`, `/resume`. All restricted to the allow-listed user.
3. In `XGROWTH_DRY_RUN=1` the engager is a no-op, so you can do the full approval
   round-trip without sending anything live.

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
PYTHONPATH=src python -m pytest -q     # 101 tests, no network/keys needed
python -m ruff check .                  # lint
```

The LLM, GitHub source, and all three X surfaces (poster, reader, engager) are
injectable, so the whole pipeline — including the approval round-trip — runs offline
in tests via fakes. `python-telegram-bot` is only needed to actually run the bot;
the decision logic is tested without it.

## Roadmap

- **Phase 1 — done:** git watcher, secret scrubber, content generator, scheduler,
  poster, audit log, cost tracker, kill switch.
- **Phase 2 — done:** read-only monitor + reply drafter + Telegram approval bot + the
  engagement gate (per-item human approval tokens) + paced follow candidates +
  static guardrail test.
- **Phase 3 — done:** analytics pull + feedback into the generator/scheduler +
  live-reply notifier.
- **Phase 4:** hardening — expanded guardrail/observability, cost dashboard, docs.

See `/root/.claude/plans/x-growth-engine-structured-waffle.md` for the full plan.
