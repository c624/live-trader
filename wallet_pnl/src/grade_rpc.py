"""Grade the shortlisted wallets: base rate first, then money, then copyability.

Appearing early in six winners sounds like skill and usually is not. A wallet
that buys every launch is early in six winners by construction, and in six
hundred losers too. So the first thing computed here is how many tokens the
wallet touched at all, because a hit rate is the only thing that makes the
winner count mean anything -- and mistaking a sprayer for a picker is exactly
the error this project already made once.

Then the money, through the same ledger that took a wallet from +234% to
+0.5% once sells with no matching purchase were excluded. Then whether the
behaviour could be followed at all: last time every profitable wallet traded
86 to 489 times a day and held for two to six minutes, which is not
something a copier one second behind can ride.

Usage: python -m wallet_pnl.src.grade_rpc <wallets.json> [max_wallets]
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from live_trader.src.chain import Rpc
from wallet_pnl.src.ledger import Swap, build_report
from wallet_pnl.src.rpcparse import swaps_for

PAUSE = 0.12
MAX_SIGNATURES = 1000


def wallet_swaps(rpc: Rpc, wallet: str) -> list[Swap]:
    rows = rpc._call("getSignaturesForAddress",
                     [wallet, {"limit": MAX_SIGNATURES}])
    if not isinstance(rows, list):
        return []
    swaps: list[Swap] = []
    for row in rows:
        sig = row.get("signature")
        if not sig or row.get("err"):
            continue
        tx = rpc._call("getTransaction",
                       [sig, {"encoding": "jsonParsed",
                              "maxSupportedTransactionVersion": 0}])
        time.sleep(PAUSE)
        if not tx:
            continue
        for leg in swaps_for(tx, wallet):
            if leg["ts"]:
                swaps.append(Swap(ts=leg["ts"], signature=leg["signature"],
                                  mint=leg["mint"],
                                  token_amount=leg["token_amount"],
                                  sol_amount=leg["sol_amount"]))
    return swaps


def main() -> None:
    found = json.loads(Path(sys.argv[1]).read_text())
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    wallets = list(found)[:limit]

    rpc = Rpc()
    print(f"{'wallet':46} {'wins':>4} {'tokens':>7} {'hit%':>6} "
          f"{'realized%':>10} {'harsh%':>8} {'hold_min':>9} {'per_day':>8}")
    for wallet in wallets:
        winners = len({r["mint"] for r in found[wallet]})
        swaps = wallet_swaps(rpc, wallet)
        if not swaps:
            print(f"{wallet:46} {winners:4d}  no readable history")
            continue
        report = build_report(wallet, swaps)
        tokens = len(report.scored)
        # The gate that matters. Six winners out of six thousand buys is a
        # sprayer; six out of thirty is worth a second look.
        hit = 100 * winners / tokens if tokens else 0.0
        days = max(report.observed_days, 0.01)
        print(f"{wallet:46} {winners:4d} {tokens:7d} {hit:5.1f}% "
              f"{report.realized_pct:+9.1f}% {report.harsh_pct:+7.1f}% "
              f"{report.median_hold_minutes:8.1f} {tokens / days:7.1f}")


if __name__ == "__main__":
    main()
