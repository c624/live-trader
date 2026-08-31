"""Spot new launches by watching for mints that have not traded before.

Looking for a CREATE_POOL label failed because the parsed feed does not emit
one. It does not need to: a mint appearing in swap traffic for the first time
is a launch, and it is a launch that already has a route, which is what the
old reserve_usd filter was really a proxy for. Nothing here depends on a
third party labelling a transaction correctly.

The measurement is the point. For each newly seen mint the age is taken from
the pool's own first trade, so this reports how late we are, not merely that
we noticed. The feed's own freshness floor is about eleven seconds, and the
measured edge is +6.9% at a thirty-second entry against -7.6% at sixty, so
the number that matters is whether detection lands in the teens or the
minutes.

Usage: python -m src.mintwatch [minutes] [poll_seconds]
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx

RPC = "https://mainnet.helius-rpc.com/?api-key={key}"
PARSE = "https://api.helius.xyz/v0/transactions?api-key={key}"
PUMP_AMM = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}


class MintWatch:
    def __init__(self, key: str):
        self._key = key
        self._client = httpx.AsyncClient(timeout=60.0)
        self.first_trade: dict[str, int] = {}   # mint -> earliest trade seen
        self.first_seen: dict[str, float] = {}  # mint -> when we noticed
        self.warmed = False

    async def close(self) -> None:
        await self._client.aclose()

    async def _signatures(self, limit: int) -> list[dict]:
        try:
            response = await self._client.post(RPC.format(key=self._key), json={
                "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
                "params": [PUMP_AMM, {"limit": limit}],
            })
            payload = response.json()
        except (httpx.HTTPError, ValueError):
            return []
        if "error" in payload:
            print("  RPC error:", str(payload["error"])[:150], flush=True)
            return []
        # Failed transactions are kept: they still name the mint being traded,
        # and dropping them is what made an earlier survey read three rows.
        return payload.get("result") or []

    async def _parse(self, sigs: list[str]) -> list[dict]:
        out: list[dict] = []
        for start in range(0, len(sigs), 100):
            try:
                response = await self._client.post(
                    PARSE.format(key=self._key),
                    json={"transactions": sigs[start:start + 100]},
                )
                batch = response.json()
            except (httpx.HTTPError, ValueError):
                continue
            if isinstance(batch, list):
                out.extend(t for t in batch if isinstance(t, dict))
        return out

    async def tick(self, limit: int) -> list[tuple[str, float]]:
        """Returns [(mint, seconds_late)] for mints seen for the first time."""
        rows = await self._signatures(limit)
        if not rows:
            return []
        parsed = await self._parse([r["signature"] for r in rows])
        now = time.time()
        fresh: list[tuple[str, float]] = []
        for tx in sorted(parsed, key=lambda t: t.get("timestamp") or 0):
            stamp = tx.get("timestamp") or 0
            for transfer in tx.get("tokenTransfers") or []:
                mint = transfer.get("mint")
                if not mint or mint == WSOL or mint in STABLES:
                    continue
                if mint in self.first_trade:
                    self.first_trade[mint] = min(self.first_trade[mint], stamp)
                    continue
                self.first_trade[mint] = stamp
                self.first_seen[mint] = now
                # The opening poll is all history; those mints were not
                # discovered, they were merely inherited.
                if self.warmed:
                    fresh.append((mint, now - stamp))
        return fresh


async def main() -> None:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY is not set")
    minutes = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
    poll_seconds = float(sys.argv[2]) if len(sys.argv) > 2 else 10.0

    watch = MintWatch(key)
    deadline = time.time() + minutes * 60
    lags: list[float] = []
    print(f"watching for {minutes:.0f} minutes, polling every {poll_seconds:.0f}s\n", flush=True)
    try:
        # Warm-up: everything already trading is history, not a discovery.
        await watch.tick(1000)
        watch.warmed = True
        print(f"warm-up: {len(watch.first_trade)} mints already trading\n", flush=True)

        ticks = 0
        while time.time() < deadline:
            fresh = await watch.tick(1000)
            ticks += 1
            for mint, late in fresh:
                # A mint whose first trade is hours old is one that fell off
                # the end of the page, not a launch we caught.
                if late <= 600:
                    lags.append(late)
            if ticks % 6 == 0:
                print(f"  tick {ticks}: {len(lags)} launches caught, "
                      f"{len(watch.first_trade)} mints known", flush=True)
            await asyncio.sleep(poll_seconds)
    finally:
        await watch.close()

    print("\n=== TIME FROM A POOL'S FIRST TRADE TO US SEEING IT ===")
    if not lags:
        print("nothing caught; either the poll is too slow to keep up with the "
              "feed or the window was too short")
        return
    lags.sort()
    def pct(p):
        return lags[min(len(lags) - 1, int(len(lags) * p / 100))]
    print(f"n={len(lags)}  fastest {lags[0]:.0f}s  p25 {pct(25):.0f}s  "
          f"median {pct(50):.0f}s  p75 {pct(75):.0f}s  slowest {lags[-1]:.0f}s")
    print(f"\nlaunches per minute caught: {len(lags) / minutes:.1f}")
    print("edge is +6.9% at a 30s entry, -7.6% at 60s; add 10-20s of quoting,")
    print("signing and landing to the median above to get real entry time.")


if __name__ == "__main__":
    asyncio.run(main())
