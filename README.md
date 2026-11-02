# fairline

An agent-run market-making desk for sports prediction markets. It borrows the sharpest price in the world, rents it out on Kalshi, and uses a team of AI agents to keep it honest and fast. Phase 0 is six weeks of building and measuring with no money at risk.

**Status:** Phase 0, Week 4 (Oct 27–Nov 2, 2026). The recorders run on an always-on server; the gates are read from Nov 2. Nothing in this repo places orders.

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
| `tape/ctl.py` | Starts, stops and checks every service as a background process that survives VS Code restarts (under systemd on EC2). |
| `infra/` | The EC2 recorder: `fl` drives it from the laptop, `setup.sh` and `fairline@.service` run on the server. |
| `tape/manifest.py` | Run on the server: which tape files are finished, and the frontier before which every Kalshi stream is complete. |
| `tape/pull.py` | The laptop's `pull` service: the recorders' state every minute, the finished tape every 10 minutes. |
| `board/export_site.py` + `site/` | The public project site and the aggregate figures it shows (see [The project site](#the-project-site)). |
| `board/indexer.py` | Follows the tape and keeps `data/board.db` current: books, trades, Pinnacle fair values, health. |
| `board/server.py` + `board/static/` | The live dashboard at http://127.0.0.1:8765 and its JSON API. |
| `board/paper.py` | Paper trading over the tape: two rule ladders replayed minute by minute before kickoff, scored at Kalshi settlement (wins, losses, P&L, CLV); also measures the G2 gap. |
| `board/tracker.py` + `board/static/tracker.js` | The Tracker tab: the paper record, the recorders right now, and history by day and by game (see below). |

Planned in the brief and not built yet: `ledger/`, `sim/`, `agents/`, `gateway/`, `evals/`, `dashboards/`.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
cp .env.example .env        # then fill in keys; .env and secrets/ are git-ignored
.venv/bin/python -m pytest
```

Put Kalshi private keys in `secrets/` (e.g. `secrets/kalshi-prod.pem`) and point `.env` at them.

## Running everything

```bash
.venv/bin/python -m tape.ctl start     # board + pull (recorders too, if there is no EC2 server)
.venv/bin/python -m tape.ctl status    # what's running, uptime, last data, last log line
open http://127.0.0.1:8765             # the board
.venv/bin/python -m tape.ctl stop      # stop everything (data is kept)
```

Or in VS Code: **Terminal → Run Task…** → *Start everything*, *Status*, *Open board*, *Restart board*, *Follow a log*, *Stop everything*.
Services run detached from the terminal, so closing or updating VS Code no longer stops recording. Logs: `data/logs/<service>.log`.

## Running on EC2

The laptop sleeps, so the recorders live on a small always-on server: a t4g.micro running Ubuntu 24.04 in us-east-1, with a 60 GB encrypted disk and a fixed IP (about $14 a month). The disk was grown from 20 GB on Oct 29, online: the tape adds about 3 GB a week, so 60 GB lasts into February. systemd runs each recorder as `fairline@<name>`, restarts it if it exits and starts it on boot. The board and tracker stay on the laptop. The `pull` service fetches the recorders' state from the server every minute (shown in the Tracker's **Now** section and the board's status chips) and copies the tape into `data/` every 10 minutes, straight away after the laptop wakes. The server decides what may be copied (`tape/manifest.py`): only hours that are finished, never a file still being written or compressed, plus a frontier, the time before which every Kalshi stream is complete. The board's indexer applies whole hours up to that frontier, all streams merged in the order they were received, so one stream's hour landing first never runs ahead of another's. The board therefore runs up to about an hour behind the server. SSH takes only the ubuntu user's key (`infra/sshd-fairline.conf`). By default it is open only to the IPs you allow, so a new network locks the pull out until `infra/fl allow-ip`, which needs an AWS session. `infra/fl open-ssh` opens it to any address instead.

```bash
aws login                   # once per AWS session; only up and the firewall commands need it
infra/fl up                 # instance, SSH key (~/.ssh/fairline.pem), firewall, fixed IP
infra/fl install            # code, .env and secrets/ to the server; Python, swap, Eastern time, systemd
infra/fl cutover            # stop the laptop's recorders, hand the open hour over, start them on the server
```

Day to day: `infra/fl status`, `infra/fl logs kalshi-live`, `infra/fl deploy` after changing recorder code (restarts them one at a time), `infra/fl sync-code` for changes that need no restart, `infra/fl pull` to copy the tape down now, `infra/fl allow-ip` after changing networks (`infra/fl forget-ip IP` drops an old one; `infra/fl forget-ip 0.0.0.0/0` undoes `open-ssh`). The same are under **Run Task… → EC2: …**. Once the server exists, `tape.ctl start` on the laptop starts the board and `pull` only; name a recorder to run it here as a fallback (that spends Odds API credits twice while the server also records).

The instance has termination protection and its disk is kept if the instance is ever deleted. The Kalshi key copied to the server can trade: SSH takes only the key in ~/.ssh/fairline.pem (no passwords, no root, three tries per connection), and `.env` and `secrets/` are readable only by the ubuntu user.

## The board

A live trading screen over everything recorded: every NFL game, spread and total market with bid, ask, mid, size, last trade, today's volume and the change since the day's first quote; Pinnacle fair value and the gap to Kalshi wherever Pinnacle offers the identical line; a per-market chart (bid/ask band, mid, fair value, trades), the order book, the strike ladder, and a scrolling tape of moves and trades.

- `data/board.db` (SQLite) is derived entirely from the tape. Rebuild it any time: `.venv/bin/python -m board.indexer --rebuild` (about 15 minutes for the whole tape; stop the `board` service first).
- Open the same file in any SQLite viewer for spreadsheet-style browsing. Tables: `market_state` (live), `top_of_book`, `trades`, `fair_history`, `games`, `markets`, `health`.
- The server listens on 127.0.0.1 only; the data never leaves this machine.

## The tracker

The board's **Tracker** tab (http://127.0.0.1:8765/#tracker, or press `2`) shows only what the code produces. Nothing on it is typed in by hand.

| Section | What it shows |
|---|---|
| **Record** | Paper bets the rules would have placed on the recorded tape, scored when Kalshi settled each market: W–L–T, P&L after fees, ROI, closing-line value (with a 95% interval), share that beat the close, the last 20 results, running P&L against what the bets were worth when placed, splits, and every bet. |
| **Now** | Each service's state, the live recorder's last minute, upcoming games, and every market's edge right now after the taker fee. |
| **History** | When the recorder could see (a 7-day chart of up and blind time against game windows), a table by day (messages, trades, data, uptime, crossed books, gap hours, bets, P&L), the Pinnacle-vs-Kalshi gap distribution, every game with Kalshi's and Pinnacle's closing prices, and the measures against the bars in `research/registry/`. |

How the paper record is made (`board/paper.py`):

- **Two rules, each at a fixed ladder of thresholds.** *Take* buys at the Kalshi price when devigged Pinnacle beats it by 0, 0.5, 1, 2 or 3¢ after the taker fee. *Quote* rests a bid and an offer 0.5, 1, 2 or 3¢ either side of fair, filled only when a trade prints through the quote (G4's trade-through rule). The 2¢ variants are the headline; the ladder shows where any edge appears. Click a rule to see its bets (or link to it: `#tracker/take@1`).
- **Sizing and settlement.** 100 contracts a bet (fewer when the book showed less), one bet per market (one fill per side for quotes), held to settlement. Winner bets use the home team's market only. Kalshi's settled result is fetched from the public API once per market and cached in `data/tracker/settlements.json`. An NFL tie settles winner markets at 50¢ a side and counts as a tie.
- **No look-ahead and no stale prices.**
  - A Pinnacle price counts from the moment the poller received it.
  - A Pinnacle line the Odds API hadn't refreshed for 5 minutes counts as pulled.
  - The rules only act on a price under 5 minutes old, so mostly in the 3 hours before kickoff.
  - Minutes when the recorder was blind, or the recorded book was crossed, are skipped.
- Numbers recompute every 5 minutes in the background (the tape scan hourly). **Recompute** forces both.
- Recorder uptime comes from the live recorder's once-a-minute heartbeat. Down means disconnected, or no sign of life for 3+ minutes (process stopped, Mac asleep, network gone).
- Keys: `1` Markets · `2` Tracker · `↑` `↓` change rule.

## Recording (individually)

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

Run the recorders in separate terminals and leave them running. Files land in `data/raw/<stream>/<YYYY-MM-DD>/<HH>.jsonl` (UTC), gzipped when the recorder moves on to the next hour (for the odds poller, at its next poll, up to 30 minutes later). A sleeping laptop stops recording; see [Running on EC2](#running-on-ec2).

## Facts the code relies on (checked 2026-10-06)

- Kalshi REST: `https://external-api.kalshi.com/trade-api/v2`; WebSocket: `wss://external-api-ws.kalshi.com/trade-api/ws/v2`, handshake signed as `GET /trade-api/ws/v2`.
- Core NFL, NBA, college football and NHL series are `quadratic_with_maker_fees`, multiplier 1: **makers pay a fee on these markets** (0.0175·p·(1−p), 0.44¢ per contract at 50¢), a quarter of the taker fee. `fairvalue/fees.py` reads this per series; never assume zero.
- The Odds API bills up to 10 bookmakers as one region, so Pinnacle plus 7 US books costs 3 credits per call. Pinnacle odds there come from Pinnacle's public site and may lag.

## Rules

- Raw market data never goes in git. Kalshi's Developer Agreement §3.1 bars redistributing API data; publish code and aggregate results only.
- No agent gets a trading tool. Pricing and order logic stay deterministic and tested.
- Gates are changed only by adding a new gate ID with a written reason.

## The project site

`site/` is a public page about Fairline: the desk, the tape, a working fair-value calculator, the gap between Kalshi and Pinnacle counted down to kickoff, the paper record and the gates. It is static, so it can be opened from the folder or hosted anywhere (GitHub Pages needs the repo public, or a paid plan).

```bash
.venv/bin/python -m board.export_site    # refresh site/data.js from the running dashboard
open site/index.html
```

`site/data.js` holds aggregates only (counts, uptime by day, gap statistics by five-minute bucket, the record by rule, the gate registry), never a ticker, price or quote, so it can be committed and published under the rule above. The scroll behaviour comes from the scroll-craft engine copied into `site/` (`scrollcraft.js`, `scrollcraft.css`); check its licence before publishing.

