"""Primary recorder: Kalshi WebSocket order books and public trades, sequence-checked, written raw.

    python -m tape.recorder_ws --league nfl --days 6

Needs a Kalshi API key (the WebSocket handshake is authenticated even for public channels).
Until you have one, run tape.recorder_rest instead.

Every message is written as received, wrapped as {"recv_ts_ms": ..., "src": "kalshi_ws", "m": <message>}.
On a sequence gap the subscription is dropped and re-subscribed for every market, which delivers
fresh snapshots; a "_gap" marker is written so analysis can see exactly where the tape was repaired.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import ssl
import time

import certifi
import websockets

from .book import SeqTracker
from .config import KALSHI, LEAGUES, WS_SIGN_PATH, data_dir, kalshi_creds
from .discover import discover_or_keep
from .kalshi_auth import auth_headers, load_private_key
from .kalshi_rest import KalshiREST
from .writer import JsonlWriter, now_ms

log = logging.getLogger("recorder_ws")
# Use certifi's CA bundle: python.org builds on macOS ship without system certificates.
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
CHANNELS = ("orderbook_delta", "trade")


class Session:
    """One WebSocket connection: one subscription per channel covering every market.

    Kalshi merges all subscriptions to the same channel on a connection into a single sid, and
    its "ok" replies consume that sid's sequence numbers (observed 2026-10-07). So the sequence
    check covers every message carrying a sid and seq, and a repair re-subscribes all markets.
    """

    def __init__(self, ws, tickers: list[str], writer: JsonlWriter):
        self.ws = ws
        self.writer = writer
        self.tickers = tickers
        self.ids = itertools.count(1)
        self.sid_channel: dict[int, str] = {}
        self.dead_sids: set[int] = set()
        self.seq = SeqTracker()
        self.stats = {"msgs": 0, "deltas": 0, "trades": 0, "gaps": 0, "errors": 0}

    async def send(self, cmd: str, params: dict) -> int:
        cid = next(self.ids)
        await self.ws.send(json.dumps({"id": cid, "cmd": cmd, "params": params}))
        return cid

    async def subscribe(self, channel: str) -> None:
        await self.send("subscribe", {"channels": [channel], "market_tickers": self.tickers})

    async def start(self) -> None:
        for channel in CHANNELS:
            await self.subscribe(channel)

    async def repair(self, sid: int, seq: int, recv: int) -> None:
        channel = self.sid_channel.pop(sid, "orderbook_delta")
        self.stats["gaps"] += 1
        self.writer.write({"recv_ts_ms": recv, "src": "recorder", "type": "_gap", "sid": sid, "seq": seq, "channel": channel})
        self.dead_sids.add(sid)
        self.seq.reset(sid)
        await self.send("unsubscribe", {"sids": [sid]})
        await self.subscribe(channel)
        log.warning("seq gap on %s (sid %s, seq %s); re-subscribed all %d markets", channel, sid, seq, len(self.tickers))

    async def handle(self, raw: str) -> None:
        recv = now_ms()
        data = json.loads(raw)
        self.writer.write({"recv_ts_ms": recv, "src": "kalshi_ws", "m": data})
        self.stats["msgs"] += 1
        kind = data.get("type")
        if kind == "subscribed":
            msg = data.get("msg", {})
            if msg.get("sid") is not None:
                self.sid_channel[msg["sid"]] = msg.get("channel", "")
        elif kind in ("orderbook_snapshot", "orderbook_delta"):
            self.stats["deltas"] += 1
        elif kind == "trade":
            self.stats["trades"] += 1
        elif kind == "error":
            self.stats["errors"] += 1
            log.error("server error: %s", data)
        sid, seq = data.get("sid"), data.get("seq")
        if sid is None or seq is None or sid in self.dead_sids:
            return
        if not self.seq.check(sid, seq):
            await self.repair(sid, seq, recv)

    async def run(self, until_monotonic: float) -> None:
        await self.start()
        next_report = time.monotonic() + 60
        while time.monotonic() < until_monotonic:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=5)
            except asyncio.TimeoutError:
                raw = None
            if raw is not None:
                await self.handle(raw)
            if time.monotonic() >= next_report:
                log.info("last 60s: %s", self.stats)
                # Heartbeat in the tape itself: proves the session was alive even when no market moved.
                self.writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_heartbeat", "stats": self.stats})
                self.stats = dict.fromkeys(self.stats, 0)
                next_report = time.monotonic() + 60


async def run(args: argparse.Namespace) -> None:
    creds = kalshi_creds(args.env)
    if creds is None:
        raise SystemExit(
            f"No Kalshi {args.env} API key configured. Add KALSHI_{args.env.upper()}_KEY_ID and "
            f"KALSHI_{args.env.upper()}_PRIVATE_KEY_PATH to .env, or run `python -m tape.recorder_rest` meanwhile."
        )
    private_key = load_private_key(creds.private_key_path)
    rest = KalshiREST(args.env)
    writer = JsonlWriter(data_dir(), "kalshi_ws")
    markets: list[dict] = []
    next_discovery = 0.0
    backoff = 1.0
    try:
        # Never exits on its own: every failure is recorded, backed off and retried.
        while True:
            if time.monotonic() >= next_discovery:
                found, refreshed = await discover_or_keep(rest, args.league, args.days, markets, log)
                if refreshed:
                    markets = found
                    writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_markets", "markets": markets})
                    next_discovery = time.monotonic() + args.refresh_min * 60
                else:
                    writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_discovery_error"})
                    next_discovery = time.monotonic() + 60
            tickers = sorted({m["ticker"] for m in markets})
            if not tickers:
                wait = 600 if next_discovery - time.monotonic() > 60 else 60
                log.info("no markets to record yet; checking again in %ds", wait)
                await asyncio.sleep(wait)
                next_discovery = 0.0
                continue
            headers = auth_headers(creds.key_id, private_key, "GET", WS_SIGN_PATH)
            try:
                async with websockets.connect(
                    KALSHI[args.env]["ws"],
                    additional_headers=headers,
                    ssl=SSL_CONTEXT,
                    ping_interval=20,
                    ping_timeout=60,  # 20 s dropped the connection on brief Wi-Fi stalls
                    max_size=2**24,
                ) as ws:
                    writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_connected"})
                    log.info("recording %d markets", len(tickers))
                    backoff = 1.0
                    await Session(ws, tickers, writer).run(next_discovery)
                    # The planned refresh: say so, so the seconds until the next _connected count as blind.
                    writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_disconnected", "reason": "refresh"})
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_disconnected", "error": repr(exc)})
                expected = isinstance(exc, (websockets.ConnectionClosed, OSError, websockets.InvalidStatus, asyncio.TimeoutError))
                log.warning("connection lost (%r); reconnecting in %.0fs", exc, backoff, exc_info=not expected)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
    finally:
        writer.close()
        await rest.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--league", nargs="+", default=["nfl"], choices=sorted(LEAGUES))
    parser.add_argument("--days", type=int, default=6)
    parser.add_argument("--env", default="prod", choices=["prod", "demo"])
    parser.add_argument("--refresh-min", type=float, default=30.0, help="re-discover markets and reconnect this often")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # it logs every request URL at INFO
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
