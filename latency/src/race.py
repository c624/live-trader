"""Race two ways of learning that a pool exists.

The decay curve says the edge is worth +6.9% at a thirty-second entry and
-7.6% at sixty, and our buys land at ninety-seven. Roughly eighty-five
percent of that is not trading, it is waiting for GeckoTerminal to list the
pool at all. So the question is whether the chain itself tells us sooner.

Both sides are timed against the same reference: the pool's own creation
timestamp on chain. GeckoTerminal is polled the way the live bot polls it,
and Helius is asked directly for the newest pump.fun launches. Neither is
given an advantage the live bot would not have.

Usage: python -m src.race [minutes] [poll_seconds]
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from datetime import datetime, timezone

import httpx

GECKO = "https://api.geckoterminal.com/api/v2"
RPC = "https://mainnet.helius-rpc.com/?api-key={key}"
PUMP_FUN_PROGRAM = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def parse_iso(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Race:
    def __init__(self, key: str):
        self._key = key
        self._client = httpx.AsyncClient(timeout=30.0)
        self.gecko_seen: dict[str, float] = {}
        self.chain_seen: dict[str, float] = {}
        self.created: dict[str, float] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict | None = None):
        try:
            r = await self._client.get(url, params=params)
            return r.json() if r.is_success else None
        except (httpx.HTTPError, ValueError):
            return None

    async def _post(self, url: str, payload: dict):
        try:
            r = await self._client.post(url, json=payload)
            return r.json() if r.is_success else None
        except (httpx.HTTPError, ValueError):
            return None

    async def poll_gecko(self) -> None:
        """The live bot's own discovery path, timed."""
        now = time.time()
        for page in (1, 2):
            payload = await self._get(
                f"{GECKO}/networks/solana/new_pools", {"page": page}
            )
            for pool in (payload or {}).get("data") or []:
                attrs = pool.get("attributes") or {}
                address = attrs.get("address")
                created = parse_iso(attrs.get("pool_created_at"))
                if not address or not created:
                    continue
                self.created.setdefault(address, created)
                self.gecko_seen.setdefault(address, now)

    async def poll_chain(self) -> None:
        """Newest pump.fun launches straight from the chain, timed."""
        now = time.time()
        result = await self._post(
            RPC.format(key=self._key),
            {
                "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [PUMP_FUN_PROGRAM, {"limit": 200}],
            },
        )
        signatures = (result or {}).get("result") or []
        fresh = [
            s for s in signatures
            if not s.get("err") and (s.get("blockTime") or 0) > now - 600
        ]
        if not fresh:
            return
        parsed = await self._post(
            f"https://api.helius.xyz/v0/transactions?api-key={self._key}",
            {"transactions": [s["signature"] for s in fresh[:100]]},
        )
        for tx in parsed or []:
            if not isinstance(tx, dict) or tx.get("type") != "CREATE_POOL":
                continue
            stamp = tx.get("timestamp")
            for account in tx.get("accountData") or []:
                address = account.get("account")
                if address and stamp:
                    self.created.setdefault(address, float(stamp))
                    self.chain_seen.setdefault(address, now)


async def main() -> None:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY is not set")
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 20.0
    poll_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0

    race = Race(key)
    deadline = time.time() + minutes * 60
    print(f"racing for {minutes:.0f} minutes, polling every {poll_seconds:.0f}s\n", flush=True)
    ticks = 0
    try:
        while time.time() < deadline:
            await asyncio.gather(race.poll_gecko(), race.poll_chain())
            ticks += 1
            if ticks % 6 == 0:
                print(f"  tick {ticks}: gecko has seen {len(race.gecko_seen)}, "
                      f"chain has seen {len(race.chain_seen)}", flush=True)
            await asyncio.sleep(poll_seconds)
    finally:
        await race.close()

    def lags(seen: dict[str, float]) -> list[float]:
        out = []
        for address, when in seen.items():
            created = race.created.get(address)
            # A pool first seen on the very first poll may have existed long
            # before the race started; those say nothing about detection speed.
            if created and 0 <= when - created <= 1800:
                out.append(when - created)
        return sorted(out)

    def report(name: str, values: list[float]) -> None:
        if not values:
            print(f"{name}: nothing measurable")
            return
        def pct(p):
            return values[min(len(values) - 1, int(len(values) * p / 100))]
        print(f"{name}: n={len(values)}  "
              f"p25 {pct(25):6.0f}s  median {pct(50):6.0f}s  p75 {pct(75):6.0f}s  "
              f"fastest {values[0]:.0f}s")

    print("\n=== TIME FROM POOL CREATION TO FIRST SIGHTING ===")
    report("geckoterminal", lags(race.gecko_seen))
    report("chain (helius)", lags(race.chain_seen))
    print("\nedge is +6.9% at a 30s entry and -7.6% at 60s; add roughly 10-20s")
    print("of quoting, signing and landing to whichever detection number wins.")


if __name__ == "__main__":
    asyncio.run(main())
