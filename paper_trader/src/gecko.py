"""GeckoTerminal client for the paper trader: pool listings and candles."""

from __future__ import annotations

import asyncio

import httpx

API_ROOT = "https://api.geckoterminal.com/api/v2"
NETWORK = "solana"


class Gecko:
    def __init__(self, calls_per_minute: float = 25.0):
        self._client = httpx.AsyncClient(
            timeout=30.0, headers={"Accept": "application/json;version=20230302"}
        )
        self._min_gap = 60.0 / calls_per_minute
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict | None = None) -> dict | None:
        async with self._lock:
            wait = self._min_gap - (asyncio.get_event_loop().time() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()
        for attempt in range(3):
            try:
                response = await self._client.get(f"{API_ROOT}{path}", params=params)
            except httpx.HTTPError:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if response.status_code == 429:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if response.status_code == 404:
                return None
            if response.is_success:
                return response.json()
            await asyncio.sleep(2 * (attempt + 1))
        return None

    async def pools(self, kind: str, pages: int) -> list[dict]:
        """kind is 'trending_pools' or 'new_pools'; returns normalized rows."""
        rows: list[dict] = []
        for page in range(1, pages + 1):
            payload = await self._get(f"/networks/{NETWORK}/{kind}", {"page": page})
            if not payload:
                break
            for pool in payload.get("data") or []:
                attrs = pool.get("attributes") or {}
                relationships = pool.get("relationships") or {}
                base = ((relationships.get("base_token") or {}).get("data") or {}).get("id", "")
                token = base.split("_", 1)[-1] if base else ""
                if not token:
                    continue
                name = attrs.get("name") or ""
                rows.append(
                    {
                        "token": token,
                        "symbol": name.split(" / ")[0] if name else "?",
                        "pool": attrs.get("address") or "",
                        "pool_created_at": attrs.get("pool_created_at"),
                        "reserve_usd": _f(attrs.get("reserve_in_usd")),
                        "price_usd": _f(attrs.get("base_token_price_usd")),
                        "volume_h1_usd": _f((attrs.get("volume_usd") or {}).get("h1")),
                    }
                )
        return rows

    async def candles(self, pool: str, before_timestamp: int) -> list[tuple]:
        payload = await self._get(
            f"/networks/{NETWORK}/pools/{pool}/ohlcv/minute",
            {
                "before_timestamp": before_timestamp,
                "limit": 1000,
                "currency": "usd",
                "aggregate": 5,
            },
        )
        if not payload:
            return []
        raw = ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        rows = [
            (int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]))
            for r in raw
            if len(r) >= 5
        ]
        return sorted(rows, key=lambda r: r[0])


    async def candles_with_volume(self, pool: str, before_timestamp: int) -> list[tuple]:
        """Same bars as candles() but keeping the volume column.

        Volume before entry is the only evidence of whether anyone other
        than the deployer was trading the pool, so the study needs it.
        """
        payload = await self._get(
            f"/networks/{NETWORK}/pools/{pool}/ohlcv/minute",
            {
                "before_timestamp": before_timestamp,
                "limit": 1000,
                "currency": "usd",
                "aggregate": 5,
            },
        )
        if not payload:
            return []
        raw = ((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        rows = [
            (
                int(r[0]),
                float(r[1]),
                float(r[2]),
                float(r[3]),
                float(r[4]),
                float(r[5]) if len(r) > 5 and r[5] is not None else 0.0,
            )
            for r in raw
            if len(r) >= 5
        ]
        return sorted(rows, key=lambda r: r[0])


def _f(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
