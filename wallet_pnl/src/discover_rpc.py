"""Which wallets were early in more than one token that ran? (keyless)

Kept separate from discover.py, which does the same job through Helius and
is still what the wallet tests exercise. Two modules for one question is
worth it while the Helius plan is exhausted and this one runs on endpoints
that cost nothing.

The tokens come from our own paper record rather than someone's list: 2,627
launches were bought at real quoted prices and followed to an exit, so the
ones that went up are known first-hand. A wallet list handed over by a
website was wrong last time in a way that took a day to notice, and this
cannot be wrong in that way.

Being early in one winner is luck; the whole population of a launch is early
in it. Being early in several is the only thing worth a second look, and even
that is a starting point for grading rather than a finding.

Usage: python -m wallet_pnl.src.discover_rpc <winners.json> [out.json] [per_token]
"""

from __future__ import annotations

import collections
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from live_trader.src.chain import Rpc
from wallet_pnl.src.rpcparse import buys_in

EARLY_SECONDS = 300     # how much of a token's life counts as early
PAUSE = 0.12            # between calls, to stay welcome on a free endpoint


def oldest_signatures(rpc: Rpc, mint: str, want: int) -> list[dict]:
    """The first transactions on a mint, oldest last in the API's order."""
    rows = rpc._call("getSignaturesForAddress", [mint, {"limit": 1000}])
    if not isinstance(rows, list) or not rows:
        return []
    rows = [r for r in rows if r.get("blockTime")]
    rows.sort(key=lambda r: r["blockTime"])
    return rows[:want]


def early_buyers(rpc: Rpc, mint: str, per_token: int = 60) -> list[dict]:
    sigs = oldest_signatures(rpc, mint, per_token)
    if not sigs:
        return []
    start = sigs[0]["blockTime"]
    found = []
    for row in sigs:
        if row["blockTime"] - start > EARLY_SECONDS:
            break
        tx = rpc._call("getTransaction",
                       [row["signature"],
                        {"encoding": "jsonParsed",
                         "maxSupportedTransactionVersion": 0}])
        time.sleep(PAUSE)
        if not tx:
            continue
        for buy in buys_in(tx):
            if buy["mint"] == mint:
                buy["age_s"] = buy["ts"] - start if buy["ts"] else None
                found.append(buy)
    return found


def main() -> None:
    winners = json.loads(Path(sys.argv[1]).read_text())
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("wallets.json")
    per_token = int(sys.argv[3]) if len(sys.argv) > 3 else 60

    rpc = Rpc()
    hits: dict[str, list[dict]] = collections.defaultdict(list)
    for i, mint in enumerate(winners, 1):
        buys = early_buyers(rpc, mint, per_token)
        for buy in buys:
            hits[buy["wallet"]].append({"mint": mint, "sol": buy["sol"],
                                        "age_s": buy["age_s"]})
        print(f"[{i}/{len(winners)}] {mint[:10]} {len(buys)} early buys",
              flush=True)

    repeat = {w: rows for w, rows in hits.items()
              if len({r['mint'] for r in rows}) >= 2}
    print(f"\nwallets seen early: {len(hits)}")
    print(f"seen early in two or more winners: {len(repeat)}")
    ranked = sorted(repeat.items(),
                    key=lambda kv: -len({r['mint'] for r in kv[1]}))
    for wallet, rows in ranked[:25]:
        mints = len({r["mint"] for r in rows})
        spend = sum(r["sol"] for r in rows)
        print(f"  {wallet}  {mints} winners  {spend:.3f} SOL committed")
    out_path.write_text(json.dumps(
        {w: rows for w, rows in ranked}, indent=1))
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
