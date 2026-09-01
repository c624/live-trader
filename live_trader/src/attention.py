"""Attention and momentum, read before the price has finished reflecting them.

Everything measured so far -- timing, authority flags, holder concentration,
transaction count, other wallets -- lives on the chain, and none of it
separated the launches that run from the ones that die by more than the
cost of acting on it. What it never measured is who is paying attention: a
token being boosted, a token whose buy count in the last five minutes is
climbing faster than its sell count, a token pulling volume while its
neighbours are not. That is the one input category left, and it is a
plausible source of the edge people who trade these by hand actually use.

DexScreener publishes it for free without a key: the latest boosted tokens,
and for any token the pair's five-minute and hourly buys, sells, volume and
price change. The rows produced here carry the same fields the arm runner
already understands, plus the attention fields, so the existing machinery
tests the idea without changes to how it scores.

Nothing here is trusted. Every field is recorded on every entry and then
checked against outcomes, the same way traction was -- and traction failed.
"""

from __future__ import annotations

import time

import httpx

BASE = "https://api.dexscreener.com"
CHAIN = "solana"
BATCH = 30      # tokens per stats call, the endpoint's limit


def _f(value):
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _txns(pair: dict, window: str) -> tuple[int, int]:
    block = (pair.get("txns") or {}).get(window) or {}
    return int(block.get("buys") or 0), int(block.get("sells") or 0)


def row_from_pair(pair: dict, boosts: int = 0, source: str = "dex") -> dict | None:
    """One candidate row in the shape the arms consume, plus attention."""
    base = pair.get("baseToken") or {}
    token = base.get("address")
    if not token:
        return None
    created_ms = pair.get("pairCreatedAt")
    m5_b, m5_s = _txns(pair, "m5")
    h1_b, h1_s = _txns(pair, "h1")
    volume = pair.get("volume") or {}
    change = pair.get("priceChange") or {}
    liquidity = (pair.get("liquidity") or {}).get("usd")
    row = {
        "token": token,
        "symbol": base.get("symbol") or "?",
        "pool": pair.get("pairAddress") or "",
        "first_trade_ts": created_ms / 1000.0 if created_ms else None,
        "price_usd": _f(pair.get("priceUsd")),
        "liquidity_usd": _f(liquidity),
        "m5_buys": m5_b,
        "m5_sells": m5_s,
        "h1_buys": h1_b,
        "h1_sells": h1_s,
        # Buy pressure as a ratio, with the sell side floored at one so a
        # token with buys and no sells reads as strong rather than infinite.
        "buy_ratio_m5": round(m5_b / max(m5_s, 1), 3),
        "vol_m5_usd": _f(volume.get("m5")),
        "vol_h1_usd": _f(volume.get("h1")),
        "chg_m5_pct": _f(change.get("m5")),
        "chg_h1_pct": _f(change.get("h1")),
        "boosts": boosts,
        "source": source,
    }
    # Rows without a creation time cannot be aged and the entry filter would
    # drop them as missing data. Better to say so than to invent a time.
    return row


class Attention:
    def __init__(self, client: httpx.Client | None = None):
        self._client = client or httpx.Client(timeout=15.0)
        self.calls = 0
        self.failures = 0

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str):
        self.calls += 1
        try:
            r = self._client.get(BASE + path)
        except httpx.HTTPError:
            self.failures += 1
            return None
        if r.status_code != 200:
            self.failures += 1
            return None
        try:
            return r.json()
        except ValueError:
            self.failures += 1
            return None

    def boosted(self) -> dict[str, int]:
        """Token address -> boost amount, for tokens boosted most recently."""
        payload = self._get("/token-boosts/latest/v1")
        out: dict[str, int] = {}
        for item in payload if isinstance(payload, list) else []:
            if item.get("chainId") != CHAIN:
                continue
            addr = item.get("tokenAddress")
            if addr:
                out[addr] = int(item.get("totalAmount") or item.get("amount") or 0)
        return out

    def stats(self, tokens: list[str]) -> list[dict]:
        """The most liquid pair per token, for up to thirty tokens at once."""
        pairs: list[dict] = []
        for i in range(0, len(tokens), BATCH):
            chunk = tokens[i:i + BATCH]
            payload = self._get(f"/tokens/v1/{CHAIN}/{','.join(chunk)}")
            if isinstance(payload, list):
                pairs.extend(payload)
            time.sleep(0.25)
        best: dict[str, dict] = {}
        for pair in pairs:
            addr = (pair.get("baseToken") or {}).get("address")
            if not addr:
                continue
            liq = _f((pair.get("liquidity") or {}).get("usd")) or 0.0
            if addr not in best or liq > (_f((best[addr].get("liquidity") or {}).get("usd")) or 0.0):
                best[addr] = pair
        return list(best.values())

    def candidates(self, extra_tokens: list[str] | None = None) -> list[dict]:
        """Boosted tokens plus any others asked for, each with live stats."""
        boosts = self.boosted()
        wanted = list(dict.fromkeys(list(boosts) + list(extra_tokens or [])))
        rows = []
        for pair in self.stats(wanted):
            addr = (pair.get("baseToken") or {}).get("address")
            row = row_from_pair(pair, boosts=boosts.get(addr, 0),
                                source="boost" if addr in boosts else "dex")
            if row:
                rows.append(row)
        return rows
