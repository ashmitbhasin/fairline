"""Kalshi REST client for public market data, with a token-bucket rate limiter.

Market data endpoints (series, events, markets, order books) need no authentication.
Basic tier: 200 read tokens/s at 10 tokens per request = 20 requests/s. We stay under that.
"""
from __future__ import annotations

import asyncio
import time

import httpx

from .config import KALSHI


class TokenBucket:
    def __init__(self, rate_per_s: float, burst: float | None = None):
        self.rate = rate_per_s
        self.capacity = burst if burst is not None else rate_per_s
        self.tokens = self.capacity
        self.updated = time.monotonic()

    async def take(self, n: float = 1.0) -> None:
        while True:
            now = time.monotonic()
            self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
            self.updated = now
            if self.tokens >= n:
                self.tokens -= n
                return
            await asyncio.sleep((n - self.tokens) / self.rate)


class KalshiREST:
    def __init__(self, env: str = "prod", requests_per_s: float = 15.0, timeout_s: float = 15.0):
        self.base = KALSHI[env]["rest"]
        self.bucket = TokenBucket(requests_per_s)
        self.client = httpx.AsyncClient(timeout=timeout_s)

    async def get(self, path: str, params=None) -> dict:
        last_error: Exception | None = None
        for attempt in range(6):
            await self.bucket.take()
            try:
                resp = await self.client.get(self.base + path, params=params)
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if resp.status_code == 429 or resp.status_code >= 500:
                    last_error = httpx.HTTPStatusError(f"HTTP {resp.status_code}", request=resp.request, response=resp)
                else:
                    resp.raise_for_status()
                    return resp.json()
            await asyncio.sleep(min(0.5 * 2**attempt, 10.0))
        assert last_error is not None
        raise last_error

    async def paginate(self, path: str, key: str, params: dict) -> list[dict]:
        params = dict(params)
        items: list[dict] = []
        while True:
            page = await self.get(path, params)
            items.extend(page.get(key, []))
            cursor = page.get("cursor")
            if not cursor:
                return items
            params["cursor"] = cursor

    async def series(self, ticker: str) -> dict:
        return (await self.get(f"/series/{ticker}")).get("series", {})

    async def open_events(self, series_ticker: str) -> list[dict]:
        return await self.paginate("/events", "events", {"series_ticker": series_ticker, "status": "open", "limit": 200})

    async def markets_for_event(self, event_ticker: str) -> list[dict]:
        return await self.paginate("/markets", "markets", {"event_ticker": event_ticker, "limit": 200})

    async def orderbooks(self, tickers: list[str]) -> list[dict]:
        """Current books for up to ~50 tickers in one request (GET /markets/orderbooks)."""
        page = await self.get("/markets/orderbooks", [("tickers", t) for t in tickers])
        return page.get("orderbooks", [])

    async def close(self) -> None:
        await self.client.aclose()
