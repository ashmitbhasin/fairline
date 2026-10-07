# fairline

An agent-run market-making desk for sports prediction markets. It borrows the sharpest price in the world, rents it out on Kalshi, and uses a team of AI agents to keep it honest and fast. Phase 0 is six weeks of building and measuring with no money at risk.

**Status:** Phase 0, Week 1 (Oct 6–12, 2026). The tape is being built. Nothing in this repo places orders.

## What's here

| Path | What it does |
|---|---|
| `tape/recorder_ws.py` | Primary recorder. Kalshi WebSocket order books and public trades, sequence-checked, written raw. Needs a Kalshi API key. |
| `tape/recorder_rest.py` | Fallback recorder. Polls Kalshi's public order books every few seconds. No key needed. |
| `tape/odds_poller.py` | Pinnacle plus US sportsbook odds from The Odds API, polled faster before kickoff. |
| `tape/discover.py` | Finds the core game, spread and total markets for upcoming NFL and NBA games. |
| `tape/book.py` | Rebuilds order books from snapshots and deltas; detects sequence gaps. |
| `fairvalue/devig.py` | Margin removal: multiplicative, additive, power, Shin. |
| `fairvalue/fees.py` | Kalshi taker and maker fees, break-even, fractional Kelly. |
| `research/registry/` | Gates G1–G7 and kill criteria, committed before the data they judge. |
| `config/blocklist.yaml` | Markets the desk must never trade (Indiana University teams). |

Planned in the brief and not built yet: `ledger/`, `sim/`, `agents/`, `gateway/`, `evals/`, `infra/`, `dashboards/`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # then fill in keys; .env and secrets/ are git-ignored
.venv/bin/python -m pytest
```

Put Kalshi private keys in `secrets/` (e.g. `secrets/kalshi-prod.pem`) and point `.env` at them.

## Recording

```bash
# See what will be recorded this week
.venv/bin/python -m tape.discover --league nfl nba --days 6

# No Kalshi key yet: poll public order books (5 s near games, 60 s otherwise)
.venv/bin/python -m tape.recorder_rest --league nfl --days 6

# With a Kalshi key: every order-book change over the WebSocket
.venv/bin/python -m tape.recorder_ws --league nfl --days 6

# Odds (needs ODDS_API_KEY); 3 credits per call
.venv/bin/python -m tape.odds_poller --league nfl
```

Run the recorders in separate terminals and leave them running. Files land in `data/raw/<stream>/<YYYY-MM-DD>/<HH>.jsonl` (UTC), gzipped once each hour closes. A sleeping laptop stops recording, so keep it awake and plugged in on game days until the recorder moves to EC2 (Week 2).

## Facts the code relies on (checked 2026-10-06)

- Kalshi REST: `https://external-api.kalshi.com/trade-api/v2`; WebSocket: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, handshake signed as `GET /trade-api/ws/v2`.
- Core NFL, NBA, college football and NHL series are `quadratic_with_maker_fees`, multiplier 1: **makers pay a fee on these markets** (0.0175·p·(1−p), 0.44¢ per contract at 50¢), a quarter of the taker fee. `fairvalue/fees.py` reads this per series; never assume zero.
- The Odds API bills up to 10 bookmakers as one region, so Pinnacle plus 7 US books costs 3 credits per call. Pinnacle odds there come from Pinnacle's public site and may lag.

## Rules

- Raw market data never goes in git. Kalshi's Developer Agreement §3.1 bars redistributing API data; publish code and aggregate results only.
- No agent gets a trading tool. Pricing and order logic stay deterministic and tested.
- Gates are changed only by adding a new gate ID with a written reason.
