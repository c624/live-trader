"""Cheap on-chain features about a token, read at the moment we would buy.

The forty-five paper trades say the median trade loses about the round-trip
cost and nothing more -- the price at ten seconds is close to a coin flip --
while the mean sits at -14.8% because a handful of positions lose ninety per
cent or more. The left tail is the whole problem. Better timing does not fix
it; not buying those tokens would.

Which signal identifies them is an empirical question, and guessing has a bad
record here: 780 configurations of entry filter and exit rule all lost once
the backtest was corrected. So this reads a small set of facts that are free
to obtain and records them on every paper entry, whether or not any arm acts
on them. Once enough positions have closed, the features can be scored against
their outcomes and a filter built from what actually separates the disasters
rather than from folklore.

What it reads, in two RPC calls:

  mint_authority    can more supply be minted? An open mint can dilute a
                    holder to nothing at will
  freeze_authority  can transfers be frozen? A frozen account cannot sell
  top1_share        largest account's share of supply
  top5_share        the top five together -- concentration is what makes a
                    single seller able to end the pool

For a pump.fun token still on its bonding curve the curve itself holds most of
the supply, so concentration will read high for honest and dishonest launches
alike. That is precisely why these are recorded rather than trusted: the data
will say whether they separate anything.
"""

from __future__ import annotations

import time


class RugCheck:
    def __init__(self, rpc, ttl_seconds: float = 900.0, max_entries: int = 4000):
        self.rpc = rpc
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._cache: dict[str, tuple[float, dict | None]] = {}
        self.calls = 0
        self.hits = 0
        self.failures = 0

    def _prune(self, now: float) -> None:
        if len(self._cache) <= self.max_entries:
            return
        for mint, (ts, _) in sorted(self._cache.items(), key=lambda kv: kv[1][0]):
            if len(self._cache) <= self.max_entries // 2:
                break
            if now - ts > 0:
                del self._cache[mint]

    def features(self, mint: str, now: float | None = None) -> dict | None:
        """Facts about the mint, or None if the chain would not say."""
        now = time.time() if now is None else now
        cached = self._cache.get(mint)
        if cached and now - cached[0] < self.ttl:
            self.hits += 1
            return cached[1]

        self.calls += 1
        info = self.rpc.mint_info(mint)
        if not info:
            self.failures += 1
            self._cache[mint] = (now, None)
            return None

        out = {
            "mint_authority": bool(info.get("mintAuthority")),
            "freeze_authority": bool(info.get("freezeAuthority")),
            "decimals": info.get("decimals"),
        }
        try:
            supply = float(info.get("supply") or 0)
        except (TypeError, ValueError):
            supply = 0.0

        holders = self.rpc.largest_holders(mint) or []
        amounts = []
        for row in holders:
            try:
                amounts.append(float(row.get("amount") or 0))
            except (TypeError, ValueError):
                continue
        amounts.sort(reverse=True)
        if supply > 0 and amounts:
            out["top1_share"] = round(amounts[0] / supply, 6)
            out["top5_share"] = round(sum(amounts[:5]) / supply, 6)
        else:
            # Never guess a share from a supply we could not read: a zero here
            # would read as "perfectly distributed", the opposite of unknown.
            out["top1_share"] = None
            out["top5_share"] = None
        out["holders_sampled"] = len(amounts)

        self._cache[mint] = (now, out)
        self._prune(now)
        return out


def is_dangerous(features: dict | None, max_top1: float = 0.95) -> str | None:
    """A reason to refuse, or None. Deliberately conservative.

    Unknown is not safe, but it is not damning either: a token whose mint the
    RPC would not describe is skipped by the arms that require a check and
    accepted by the arms that do not, so the difference between the two is
    itself measurable.
    """
    if features is None:
        return "unknown"
    if features.get("mint_authority"):
        return "mint_open"
    if features.get("freeze_authority"):
        return "freezable"
    top1 = features.get("top1_share")
    if top1 is not None and top1 > max_top1:
        return "concentrated"
    return None
