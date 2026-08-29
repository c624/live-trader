"""Minimal synchronous GeckoTerminal client for the live loop.

Deliberately independent of paper_trader's async client: the live repo is a
standalone mirror and must not import across package roots.
"""

from __future__ import annotations

import time

import httpx

API_ROOT = "https://api.geckoterminal.com/api/v2"
NETWORK = "solana"


class Gecko:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(
            timeout=30.0, headers={"Accept": "application/json;version=20230302"}
        )
        self._last = 0.0

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict | None = None) -> dict | None:
        gap = 2.5 - (time.monotonic() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.monotonic()
        for attempt in range(3):
            try:
                response = self._client.get(f"{API_ROOT}{path}", params=params)
            except httpx.HTTPError:
                time.sleep(2 * (attempt + 1))
                continue
            if response.status_code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            if response.status_code == 404:
                return None
            if response.is_success:
                return response.json()
            time.sleep(2 * (attempt + 1))
        return None

    def candidate_pools(self) -> list[dict]:
        """Discovery identical to the paper lab's: trending plus new pools.

        The first pages of new_pools hold only pools seconds-to-minutes old
        during busy hours; the 15-60 minute window the lab measured lives in
        trending_pools and the deeper new_pools pages.
        """
        return self.pools("trending_pools", 3) + self.pools("new_pools", 5)

    def pools(self, kind: str, pages: int) -> list[dict]:
        rows: list[dict] = []
        for page in range(1, pages + 1):
            payload = self._get(f"/networks/{NETWORK}/{kind}", {"page": page})
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


def _f(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None
