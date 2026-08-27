"""Jupiter swap API client: quotes and swap-transaction building.

Uses the free keyless tier. Every function returns None on failure rather
than raising, because one bad token must never take down the whole loop.
"""

from __future__ import annotations

import os
import time

import httpx

BASE = os.environ.get("JUPITER_BASE", "https://lite-api.jup.ag/swap/v1")
SOL_MINT = "So11111111111111111111111111111111111111112"
USDC_MINT = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"

# Hard ceiling on priority fee per transaction: 0.002 SOL. Getting a $15
# ticket landed a block sooner is not worth more than that.
MAX_PRIORITY_LAMPORTS = 2_000_000


class Jupiter:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=20.0)

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict) -> dict | None:
        for attempt in range(3):
            try:
                response = self._client.get(f"{BASE}{path}", params=params)
            except httpx.HTTPError:
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.status_code == 429:
                time.sleep(2.0 * (attempt + 1))
                continue
            if response.is_success:
                return response.json()
            if 400 <= response.status_code < 500:
                return None
            time.sleep(1.5 * (attempt + 1))
        return None

    def quote(
        self, input_mint: str, output_mint: str, amount: int, slippage_bps: int
    ) -> dict | None:
        """amount is in the input mint's raw units (lamports for SOL)."""
        payload = self._get(
            "/quote",
            {
                "inputMint": input_mint,
                "outputMint": output_mint,
                "amount": str(amount),
                "slippageBps": slippage_bps,
                "restrictIntermediateTokens": "true",
            },
        )
        if not payload or "outAmount" not in payload:
            return None
        return payload

    def swap_transaction(self, quote: dict, pubkey: str) -> str | None:
        """Returns a base64 unsigned transaction, or None."""
        body = {
            "quoteResponse": quote,
            "userPublicKey": pubkey,
            "wrapAndUnwrapSol": True,
            "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {
                "priorityLevelWithMaxLamports": {
                    "maxLamports": MAX_PRIORITY_LAMPORTS,
                    "priorityLevel": "high",
                }
            },
        }
        for attempt in range(3):
            try:
                response = self._client.post(f"{BASE}/swap", json=body)
            except httpx.HTTPError:
                time.sleep(1.5 * (attempt + 1))
                continue
            if response.is_success:
                return (response.json() or {}).get("swapTransaction")
            if 400 <= response.status_code < 500:
                return None
            time.sleep(1.5 * (attempt + 1))
        return None

    def sol_usd(self) -> float | None:
        """Spot SOL price via a 1 SOL -> USDC quote."""
        quote = self.quote(SOL_MINT, USDC_MINT, 1_000_000_000, 50)
        if not quote:
            return None
        try:
            return int(quote["outAmount"]) / 1e6
        except (KeyError, ValueError, TypeError):
            return None


def price_impact_pct(quote: dict) -> float | None:
    try:
        return abs(float(quote.get("priceImpactPct"))) * 100.0
    except (TypeError, ValueError):
        return None
