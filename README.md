# autotwitter — X Growth Engine

A compliance-first personal X (Twitter) growth tool for building in public. It
**fully automates original posting** — the one thing X's rules allow to be automated —
and keeps a **human in the loop for every engagement** (replies, follows).

→ **Setup:** [SETUP.md](SETUP.md) (zero to running) · **Deploy:** [DEPLOY.md](DEPLOY.md)

## The safety model

X bans accounts on *behavioral patterns*, not on who clicked the button — so this tool
**never** auto-likes/follows/replies/reposts/DMs. That's structural, not a flag. There
are three separate X surfaces, and the separation *is* the safety model:

- **Poster** — original tweets + replies to *your own* tweets only.
- **Reader** — read-only search/timelines/lookups. Can't post or engage.
- **Engager** (reply-to-others, follow) — reachable **only** through an
  `engagement_gate` that requires a fresh, single-use, item-bound token, minted
  **only** by a real one-tap approval from the allow-listed Telegram user.

A static test (`tests/test_guardrail.py`) fails CI if any engagement endpoint, the
gate, or token-minting ever leaks outside those modules. Other guardrails: secrets are
scrubbed before anything reaches the model; links go in the first reply (never the
body); posting is capped, spaced, and jittered; a kill switch pauses + clears the
queue; everything is audit-logged; API spend is tracked against a weekly cap.

## What it does

- **Posts about your work** — polls your repos, picks the most post-worthy commit from
  the last N days (not every commit), and drafts it in your voice.
- **Posts about AI news** — pulls trending stories (Hacker News) and drafts a grounded
  opinion or a tie-in to your work, using Claude's web search. *(Optional.)*
- **Balances the mix** — a daily planner blends work / outside-world / tie-in posts up
  to your `posts_per_day`, and de-dupes so it never says the same thing twice.
- **Engagement, human-gated** — finds reply/follow opportunities, drafts sharp replies,
  and pushes a one-tap Approve/Skip batch to your phone via Telegram. *(Optional.)*
- **Learns over time** — pulls your own posts' metrics and steers topics, timing, and
  which accounts/topics to reply to toward what actually performs.
- **Sounds like you** — distills a writing-voice profile from your blog.

## How it works

```
commits ─┐
AI news ─┤→ classify (Haiku) → content planner → draft (Sonnet, +web search)
blog ────┘                         │                    │
                                   ├→ de-dupe + voice ───┤
                                   └→ scheduler (windows, caps, jitter) → Poster

monitor (read-only) → rank → reply drafter → Telegram Approve/Skip → engagement gate → sent
analytics (owned reads) → insights → soften drafting/timing/reply ranking
```

Everything runs as one always-on process (scheduler + Telegram bot + a localhost admin
API), needing **outbound network only**.

## Quickstart

Requires **Python 3.10+** (or just use Docker — see [DEPLOY.md](DEPLOY.md)).

```bash
cp .env.example .env                 # secrets (gitignored)
cp config.example.yaml config.yaml   # behavior (gitignored)
pip install -r requirements.txt
PYTHONPATH=src python -m xgrowth.app
```

`XGROWTH_DRY_RUN=1` (the default) posts nothing — the poster logs synthetic ids so you
can watch the pipeline safely. [SETUP.md](SETUP.md) covers credentials, the X OAuth
mint, going live, and a troubleshooting table.

Control it from Telegram (`/status`, `/now`, `/kill`, `/resume`) or the admin API
(`/health`, `/status`, `POST /admin/kill`, `POST /admin/resume`, bound to `127.0.0.1`).

## Develop

```bash
PYTHONPATH=src python -m pytest -q   # offline — no network or keys needed
python -m ruff check .
```

The LLM, GitHub source, and all three X surfaces are injectable, so the whole pipeline
— including the approval round-trip — runs offline in tests via fakes.
