"""Primary recorder: Kalshi WebSocket order books and public trades, sequence-checked, written raw.

    python -m tape.recorder_ws --league nfl --days 6

Needs a Kalshi API key (the WebSocket handshake is authenticated even for public channels).
Until you have one, run tape.recorder_rest instead.

Every message is written as received, wrapped as {"recv_ts_ms": ..., "src": "kalshi_ws", "m": <message>}.
On a sequence gap the affected subscription is dropped and re-subscribed, which delivers fresh
snapshots; a "_gap" marker is written so analysis can see exactly where the tape was repaired.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import logging
import time

import websockets

from .book import SeqTracker
from .config import KALSHI, LEAGUES, WS_SIGN_PATH, data_dir, kalshi_creds
from .discover import discover, today_et
from .kalshi_auth import auth_headers, load_private_key
from .kalshi_rest import KalshiREST
from .writer import JsonlWriter, now_ms

log = logging.getLogger("recorder_ws")
CHUNK = 100  # tickers per subscription; smaller subscriptions are less likely to overflow (error 25)


class Session:
    """One WebSocket connection: subscribe in chunks, record everything, repair gaps."""

    def __init__(self, ws, tickers: list[str], writer: JsonlWriter):
        self.ws = ws
        self.writer = writer
        self.ids = itertools.count(1)
        self.chunks = [tickers[i : i + CHUNK] for i in range(0, len(tickers), CHUNK)]
        self.pending: dict[int, tuple[str, int]] = {}  # command id -> (channel, chunk index)
        self.sid_chunk: dict[int, tuple[str, int]] = {}  # sid -> (channel, chunk index)
        self.dead_sids: set[int] = set()
        self.seq = SeqTracker()
        self.stats = {"msgs": 0, "deltas": 0, "trades": 0, "gaps": 0, "errors": 0}

    async def send(self, cmd: str, params: dict) -> int:
        cid = next(self.ids)
        await self.ws.send(json.dumps({"id": cid, "cmd": cmd, "params": params}))
        return cid

    async def subscribe(self, channel: str, idx: int) -> None:
        cid = await self.send("subscribe", {"channels": [channel], "market_tickers": self.chunks[idx]})
        self.pending[cid] = (channel, idx)

    async def start(self) -> None:
        for idx in range(len(self.chunks)):
            await self.subscribe("orderbook_delta", idx)
            await self.subscribe("trade", idx)

    async def repair(self, sid: int, seq: int, recv: int) -> None:
        self.stats["gaps"] += 1
        self.writer.write({"recv_ts_ms": recv, "src": "recorder", "type": "_gap", "sid": sid, "seq": seq})
        channel, idx = self.sid_chunk.pop(sid, ("orderbook_delta", None))
        self.dead_sids.add(sid)
        self.seq.reset(sid)
        await self.send("unsubscribe", {"sids": [sid]})
        if idx is not None:
            await self.subscribe(channel, idx)
        log.warning("seq gap on sid %s at seq %s; re-subscribed", sid, seq)

    async def handle(self, raw: str) -> None:
        recv = now_ms()
        data = json.loads(raw)
        self.writer.write({"recv_ts_ms": recv, "src": "kalshi_ws", "m": data})
        self.stats["msgs"] += 1
        kind = data.get("type")
        if kind == "subscribed":
            sid = data.get("msg", {}).get("sid")
            if data.get("id") in self.pending and sid is not None:
                self.sid_chunk[sid] = self.pending.pop(data["id"])
        elif kind in ("orderbook_snapshot", "orderbook_delta"):
            self.stats["deltas"] += 1
            sid, seq = data.get("sid"), data.get("seq")
            if sid in self.dead_sids or sid is None or seq is None:
                return
            if not self.seq.check(sid, seq):
                await self.repair(sid, seq, recv)
        elif kind == "trade":
            self.stats["trades"] += 1
        elif kind == "error":
            self.stats["errors"] += 1
            log.error("server error: %s", data)

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
    backoff = 1.0
    try:
        while True:
            markets = await discover(rest, args.league, today_et(), args.days)
            tickers = sorted({m["ticker"] for m in markets})
            writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_markets", "markets": markets})
            if not tickers:
                log.info("no open markets in window; checking again in 10 min")
                await asyncio.sleep(600)
                continue
            log.info("recording %d markets", len(tickers))
            headers = auth_headers(creds.key_id, private_key, "GET", WS_SIGN_PATH)
            try:
                async with websockets.connect(
                    KALSHI[args.env]["ws"], additional_headers=headers, ping_interval=20, ping_timeout=20, max_size=2**24
                ) as ws:
                    writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_connected"})
                    await Session(ws, tickers, writer).run(time.monotonic() + args.refresh_min * 60)
                    backoff = 1.0
            except (websockets.ConnectionClosed, OSError, websockets.InvalidStatus) as exc:
                writer.write({"recv_ts_ms": now_ms(), "src": "recorder", "type": "_disconnected", "error": repr(exc)})
                log.warning("connection lost (%r); reconnecting in %.0fs", exc, backoff)
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
