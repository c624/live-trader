"""Find candidate wallets from the winners the lab already watched.

Carter's directive: stop grading a third-party list and source wallets from
the chain itself. The FomoScan mapping failed validation - tokens arrive at
those addresses but their SOL never moves, so whoever funds the trades, it
is not them - and the pool-sniping studies left one asset behind that is
actually useful here: a few hundred pools with known outcomes, including the
~16% that genuinely doubled.

The pipeline: take those winner pools, walk each one's history back to its
birth, and record every wallet that bought in the first minutes. Being early
on one winner is luck; the earlier "early-buyer" study showed spray bots are
early on everything by base rate. So discovery only nominates wallets that
were early on MULTIPLE distinct winners, and nomination is not a grade: every
candidate then goes through the validated P&L ledger, where spray bots fail
on buys/day, dust fails on median size, and the persistence split decides
whether any of it repeats.

Usage: python -m src.discover <state_dir> [max_pools] [max_candidates]
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

import httpx

RPC_URL = "https://mainnet.helius-rpc.com/?api-key={key}"
PARSE_URL = "https://api.helius.xyz/v0/transactions?api-key={key}"
GECKO = "https://api.geckoterminal.com/api/v2"

EARLY_WINDOW_SECONDS = 30 * 60
SIGNATURE_PAGE_BUDGET = 12          # pools busier than 12k txs are skipped
MIN_DISTINCT_WINNERS = 2
DOUBLE = 2.0


class Rpc:
    def __init__(self, api_key: str, requests_per_second: float = 8.0):
        self._key = api_key
        self._client = httpx.AsyncClient(timeout=45.0)
        self._min_gap = 1.0 / requests_per_second
        self._last = 0.0
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    async def _throttle(self) -> None:
        async with self._lock:
            wait = self._min_gap - (asyncio.get_event_loop().time() - self._last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last = asyncio.get_event_loop().time()

    async def _post(self, url: str, payload) -> dict | list | None:
        await self._throttle()
        for attempt in range(3):
            try:
                response = await self._client.post(url, json=payload)
            except httpx.HTTPError:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if response.status_code == 429:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if response.is_success:
                return response.json()
            await asyncio.sleep(2 * (attempt + 1))
        return None

    async def _get(self, url: str, params: dict | None = None):
        await self._throttle()
        for attempt in range(3):
            try:
                response = await self._client.get(url, params=params)
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

    async def oldest_signatures(self, address: str) -> list[dict] | None:
        """Oldest-first signatures, or None when the budget runs out first.

        A truncated middle of a history says nothing about who was early, so
        running out of budget is a skip, never a partial answer.
        """
        pages: list[list[dict]] = []
        before = None
        for _ in range(SIGNATURE_PAGE_BUDGET):
            params = [address, {"limit": 1000}]
            if before:
                params[1]["before"] = before
            result = await self._post(
                RPC_URL.format(key=self._key),
                {"jsonrpc": "2.0", "id": 1,
                 "method": "getSignaturesForAddress", "params": params},
            )
            page = (result or {}).get("result")
            if page is None:
                return None
            pages.append(page)
            if len(page) < 1000:
                ordered = [row for p in reversed(pages) for row in reversed(p)]
                return [r for r in ordered if not r.get("err")]
            before = page[-1]["signature"]
        return None

    async def parse_batch(self, signatures: list[str]) -> list[dict]:
        out: list[dict] = []
        for start in range(0, len(signatures), 100):
            chunk = signatures[start:start + 100]
            parsed = await self._post(
                PARSE_URL.format(key=self._key), {"transactions": chunk}
            )
            if isinstance(parsed, list):
                out.extend(t for t in parsed if isinstance(t, dict))
        return out

    async def candles(self, pool: str, before_ts: int) -> list[tuple]:
        payload = await self._get(
            f"{GECKO}/networks/solana/pools/{pool}/ohlcv/minute",
            {"before_timestamp": before_ts, "limit": 1000,
             "currency": "usd", "aggregate": 5},
        )
        raw = (((payload or {}).get("data") or {}).get("attributes") or {}).get("ohlcv_list") or []
        rows = [(int(r[0]), float(r[4]), float(r[2])) for r in raw if len(r) >= 5]
        return sorted(rows)


def is_winner(rows: list[tuple], entry_ts: int) -> bool:
    """Did the pool print a clean 2x after entry, on real prices?

    rows are (ts, close, high). The entry price is the last close at or
    before entry; the position doubled if any later high reached 2x it.
    """
    entry_price = None
    for ts, close, _high in rows:
        if ts <= entry_ts:
            entry_price = close
        else:
            break
    if not entry_price:
        return False
    window_end = entry_ts + 24 * 3600
    return any(
        ts > entry_ts and ts <= window_end and high >= entry_price * DOUBLE
        for ts, _close, high in rows
    )


def early_buyers(parsed: list[dict], mint: str, launch_ts: int) -> dict[str, int]:
    """wallet -> seconds after launch, for buyers inside the early window.

    The deployer's own wallet is excluded: it is early on its own token by
    construction, and copying deployers is copying the house.
    """
    deployer = parsed[0].get("feePayer") if parsed else None
    out: dict[str, int] = {}
    for tx in parsed:
        ts = tx.get("timestamp") or 0
        if ts > launch_ts + EARLY_WINDOW_SECONDS:
            break
        for transfer in tx.get("tokenTransfers") or []:
            if transfer.get("mint") != mint:
                continue
            buyer = transfer.get("toUserAccount")
            try:
                amount = float(transfer.get("tokenAmount") or 0)
            except (TypeError, ValueError):
                continue
            if not buyer or buyer == deployer or amount <= 0:
                continue
            out.setdefault(buyer, max(0, ts - launch_ts))
    return out


def load_pools(state_dir: Path, limit: int) -> list[dict]:
    state = json.loads((state_dir / "paper_state.json").read_text())
    seen: set[str] = set()
    out: list[dict] = []
    for bucket in ("riding", "open"):
        for p in state.get(bucket, []):
            if p["pool"] in seen:
                continue
            seen.add(p["pool"])
            out.append(p)
    out.sort(key=lambda p: p["entry_ts"])
    return out[:limit]


async def main() -> None:
    key = os.environ.get("HELIUS_API_KEY")
    if not key:
        raise RuntimeError("HELIUS_API_KEY is not set")
    state_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "state")
    max_pools = int(sys.argv[2]) if len(sys.argv) > 2 else 400
    max_candidates = int(sys.argv[3]) if len(sys.argv) > 3 else 40

    pools = load_pools(state_dir, max_pools)
    print(f"screening {len(pools)} pools for 2x winners\n", flush=True)

    rpc = Rpc(key)
    hits: dict[str, set[str]] = defaultdict(set)
    earliest: dict[str, int] = {}
    winners = skipped_busy = 0
    try:
        for i, position in enumerate(pools, 1):
            rows = await rpc.candles(position["pool"], position["entry_ts"] + 26 * 3600)
            if not is_winner(rows, position["entry_ts"]):
                continue
            winners += 1
            signatures = await rpc.oldest_signatures(position["pool"])
            if signatures is None:
                skipped_busy += 1
                continue
            launch_ts = signatures[0].get("blockTime") or position["entry_ts"]
            window = [
                r["signature"] for r in signatures
                if (r.get("blockTime") or 0) <= launch_ts + EARLY_WINDOW_SECONDS
            ][:300]
            parsed = await rpc.parse_batch(window)
            parsed.sort(key=lambda t: t.get("timestamp") or 0)
            for wallet, after in early_buyers(parsed, position["token"], launch_ts).items():
                hits[wallet].add(position["token"])
                earliest[wallet] = min(earliest.get(wallet, 10**9), after)
            if i % 25 == 0:
                print(f"  ...{i}/{len(pools)} pools, {winners} winners, "
                      f"{len(hits)} wallets seen", flush=True)
    finally:
        await rpc.close()

    print(f"\nwinners found: {winners} ({skipped_busy} skipped, history too busy)")
    ranked = sorted(hits.items(), key=lambda kv: (-len(kv[1]), earliest.get(kv[0], 0)))
    qualified = [(w, mints) for w, mints in ranked if len(mints) >= MIN_DISTINCT_WINNERS]
    print(f"wallets early on >= {MIN_DISTINCT_WINNERS} distinct winners: {len(qualified)}")

    print("\n=== CANDIDATES (nomination only, not a grade) ===")
    for wallet, mints in qualified[:max_candidates]:
        print(f"CANDIDATE {wallet} winners={len(mints)} earliest_s={earliest[wallet]}")
    if not qualified:
        print("none - being early on winners repeatedly is rarer than the "
              "spray-bot base rate suggested, which is itself a finding")


if __name__ == "__main__":
    asyncio.run(main())
