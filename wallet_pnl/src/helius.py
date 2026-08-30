"""Wallet swap history from Helius, both sides of every trade.

The older client in the copy-trade actor parsed only purchases, because it
only needed an entry to simulate against. A profit-and-loss ledger needs the
sells too, so this returns signed legs: what the wallet gave up and what came
back, in SOL terms.

Swaps quoted in stablecoins are counted but not priced, because converting
them would need a SOL/USD history we do not have. The share of activity that
was skipped is reported so a wallet whose numbers are badly incomplete can be
discarded rather than quietly mis-graded.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timedelta, timezone

import httpx

from .ledger import Swap

API_URL = "https://api.helius.xyz/v0/addresses/{address}/transactions"
WSOL = "So11111111111111111111111111111111111111112"
STABLES = {
    "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
    "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB",
}
LAMPORTS_PER_SOL = 1_000_000_000
PAGE_CAP = 60


class HeliusSwaps:
    def __init__(self, api_key: str | None = None, requests_per_second: float = 8.0):
        key = api_key or os.environ.get("HELIUS_API_KEY")
        if not key:
            raise RuntimeError("HELIUS_API_KEY is not set")
        self._key = key
        self._client = httpx.AsyncClient(timeout=45.0)
        self._min_gap = 1.0 / requests_per_second
        self._lock = asyncio.Lock()
        self._last = 0.0
        self.skipped_stable = 0
        self.skipped_unparsed = 0
        # Which path produced each swap. Three parsers have now disagreed
        # with reality in a row; a counter is cheaper than another guess.
        self.by_path = {"balances": 0, "event": 0, "transfers": 0}
        self.shape_sample: dict | None = None

    async def close(self) -> None:
        await self._client.aclose()

    async def _page(self, wallet: str, before: str | None) -> list[dict]:
        async with self._lock:
            gap = asyncio.get_event_loop().time() - self._last
            if gap < self._min_gap:
                await asyncio.sleep(self._min_gap - gap)
            self._last = asyncio.get_event_loop().time()

        params = {"api-key": self._key, "limit": 100, "type": "SWAP"}
        if before:
            params["before"] = before
        for attempt in range(3):
            try:
                response = await self._client.get(
                    API_URL.format(address=wallet), params=params
                )
            except httpx.HTTPError:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if response.status_code == 429:
                await asyncio.sleep(5 * (attempt + 1))
                continue
            if response.status_code in (400, 404):
                return []
            if response.is_success:
                payload = response.json()
                return payload if isinstance(payload, list) else []
            await asyncio.sleep(2 * (attempt + 1))
        return []

    async def swaps(self, wallet: str, lookback_days: int) -> list[Swap]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).timestamp()
        out: list[Swap] = []
        before: str | None = None
        for _ in range(PAGE_CAP):
            batch = await self._page(wallet, before)
            if not batch:
                break
            stop = False
            for tx in batch:
                if (tx.get("timestamp") or 0) < cutoff:
                    stop = True
                    break
                parsed = self.parse(tx, wallet)
                if parsed:
                    out.append(parsed)
            if stop:
                break
            before = batch[-1].get("signature")
            if not before:
                break
        out.sort(key=lambda s: s.ts)
        return out

    def parse(self, tx: dict, wallet: str) -> Swap | None:
        """One transaction into a signed SOL-quoted leg, or None."""
        ts, signature = tx.get("timestamp"), tx.get("signature")
        if not ts or not signature:
            return None

        sol_delta = 0.0
        token_delta = 0.0
        mint = None
        touched_stable = False

        # Balance changes are the ground truth: they are what the wallet's
        # accounts actually gained and lost. The parsed swap event and the
        # raw transfers both miss SOL that moves through a temporary wrapped
        # -SOL account, which left the first live run measuring account rent
        # (0.002 SOL) instead of trade size for 24 of 27 wallets.
        mint, token_delta, sol_delta, saw_stable = _from_account_data(tx, wallet)
        touched_stable = touched_stable or saw_stable
        if self.shape_sample is None:
            self.shape_sample = {
                "top_level_keys": sorted(tx.keys()),
                "account_data_entries": len(tx.get("accountData") or []),
                "wallet_entry_present": any(
                    e.get("account") == wallet for e in tx.get("accountData") or []
                ),
                "wallet_native_change": next(
                    (e.get("nativeBalanceChange") for e in tx.get("accountData") or []
                     if e.get("account") == wallet), None
                ),
                "token_balance_change_owners": sorted({
                    str(c.get("userAccount"))[:6]
                    for e in tx.get("accountData") or []
                    for c in (e.get("tokenBalanceChanges") or [])
                })[:5],
                "wallet_prefix": wallet[:6],
            }
        if mint and token_delta and sol_delta and (token_delta > 0) != (sol_delta > 0):
            self.by_path["balances"] += 1
            return Swap(
                ts=int(ts),
                signature=signature,
                mint=mint,
                token_amount=token_delta,
                sol_amount=sol_delta,
            )

        sol_delta = 0.0
        token_delta = 0.0
        mint = None
        swap = (tx.get("events") or {}).get("swap") or {}
        native_in = (swap.get("nativeInput") or {}).get("amount")
        native_out = (swap.get("nativeOutput") or {}).get("amount")
        if native_in:
            sol_delta -= int(native_in) / LAMPORTS_PER_SOL
        if native_out:
            sol_delta += int(native_out) / LAMPORTS_PER_SOL

        for leg, sign in ((swap.get("tokenInputs") or [], -1), (swap.get("tokenOutputs") or [], 1)):
            for entry in leg:
                entry_mint = entry.get("mint")
                amount = _amount(entry)
                if not entry_mint or not amount:
                    continue
                if entry_mint == WSOL:
                    sol_delta += sign * amount
                elif entry_mint in STABLES:
                    touched_stable = True
                else:
                    mint = entry_mint
                    token_delta += sign * amount

        if not mint or token_delta == 0 or sol_delta == 0:
            # Helius classifies only some routers. For the rest the raw
            # transfers say the same thing: what left the wallet and what
            # arrived. Without this fallback almost every swap is discarded.
            mint, token_delta, sol_delta, saw_stable = _from_transfers(tx, wallet)
            touched_stable = touched_stable or saw_stable

        if not mint or token_delta == 0 or sol_delta == 0:
            if touched_stable:
                self.skipped_stable += 1
            else:
                self.skipped_unparsed += 1
            return None

        # A buy must cost SOL and a sell must return it; anything else is a
        # shape this parser does not understand and must not guess at.
        if (token_delta > 0) == (sol_delta > 0):
            self.skipped_unparsed += 1
            return None

        self.by_path["event" if swap else "transfers"] += 1
        return Swap(
            ts=int(ts),
            signature=signature,
            mint=mint,
            token_amount=token_delta,
            sol_amount=sol_delta,
        )


def _amount(leg: dict) -> float:
    raw = leg.get("rawTokenAmount") or {}
    amount, decimals = raw.get("tokenAmount"), raw.get("decimals")
    if amount is not None and decimals is not None:
        try:
            return int(amount) / (10 ** int(decimals))
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(leg.get("tokenAmount") or 0)
    except (TypeError, ValueError):
        return 0.0


def _from_transfers(tx: dict, wallet: str):
    """Net position change for the wallet, read from raw transfers.

    Returns (mint, token_delta, sol_delta, saw_stable). The traded token is
    the non-quote mint the wallet moved the most of, so routing hops through
    other tokens cannot be mistaken for the trade itself.
    """
    token_deltas: dict[str, float] = {}
    sol_delta = 0.0
    saw_stable = False

    for transfer in tx.get("tokenTransfers") or []:
        mint = transfer.get("mint")
        if not mint:
            continue
        try:
            amount = float(transfer.get("tokenAmount") or 0)
        except (TypeError, ValueError):
            continue
        if not amount:
            continue
        if transfer.get("toUserAccount") == wallet:
            sign = 1.0
        elif transfer.get("fromUserAccount") == wallet:
            sign = -1.0
        else:
            continue
        if mint == WSOL:
            sol_delta += sign * amount
        elif mint in STABLES:
            saw_stable = True
        else:
            token_deltas[mint] = token_deltas.get(mint, 0.0) + sign * amount

    for native in tx.get("nativeTransfers") or []:
        try:
            amount = int(native.get("amount") or 0) / LAMPORTS_PER_SOL
        except (TypeError, ValueError):
            continue
        if native.get("toUserAccount") == wallet:
            sol_delta += amount
        elif native.get("fromUserAccount") == wallet:
            sol_delta -= amount

    if not token_deltas:
        return None, 0.0, sol_delta, saw_stable
    mint = max(token_deltas, key=lambda m: abs(token_deltas[m]))
    return mint, token_deltas[mint], sol_delta, saw_stable


def _from_account_data(tx: dict, wallet: str):
    """Net SOL and token change for the wallet, from account balance deltas.

    A wallet's own native balance change plus its wrapped-SOL balance change
    is the true economic SOL delta: wrapping moves value between the two and
    nets to zero, so counting both is correct rather than double counting.
    """
    sol_delta = 0.0
    token_deltas: dict[str, float] = {}
    saw_stable = False

    for entry in tx.get("accountData") or []:
        if entry.get("account") == wallet:
            try:
                sol_delta += int(entry.get("nativeBalanceChange") or 0) / LAMPORTS_PER_SOL
            except (TypeError, ValueError):
                pass
        for change in entry.get("tokenBalanceChanges") or []:
            if change.get("userAccount") != wallet:
                continue
            mint = change.get("mint")
            amount = _amount(change)
            if not mint or not amount:
                continue
            if mint == WSOL:
                sol_delta += amount
            elif mint in STABLES:
                saw_stable = True
            else:
                token_deltas[mint] = token_deltas.get(mint, 0.0) + amount

    if not token_deltas:
        return None, 0.0, sol_delta, saw_stable
    mint = max(token_deltas, key=lambda m: abs(token_deltas[m]))
    return mint, token_deltas[mint], sol_delta, saw_stable
