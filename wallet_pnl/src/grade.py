"""Grade wallets on their own realized profit, then test whether it persists.

Two questions, in order.

First: did the wallet actually make money? Every earlier copy-trade study
skipped this and went straight to simulating our exits on their entries, so a
wallet was condemned for our rules and a bad trader could pass for a good one.
This asks the wallet's own ledger.

Second, and the only one that matters for copying: does being profitable in
one period predict being profitable in the next? A wallet that made money
once is a wallet that made money once. If profit does not persist, there is
nobody to copy, no matter how good the leaderboard looks.

Usage: python -m src.grade wallets.txt [lookback_days]
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

from .helius import HeliusSwaps
from .ledger import build_report, split_by_time

MIN_CLOSED_POSITIONS = 8      # fewer than this is an anecdote, not a record
SPRAY_BUYS_PER_DAY = 40       # above this the wallet is a bot, not a trader
DUST_SOL = 0.02               # median buy below this is not real money


def load_wallets(path: Path) -> list[tuple[str, str]]:
    """Lines of 'address' or 'address label'; blanks and # comments ignored."""
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        out.append((parts[0], parts[1] if len(parts) > 1 else parts[0][:8]))
    return out


def verdict(report) -> str:
    if len(report.closed) < MIN_CLOSED_POSITIONS:
        return "TOO_FEW_TRADES"
    if report.buys_per_day > SPRAY_BUYS_PER_DAY:
        return "SPRAY_BOT"
    if report.median_buy_sol < DUST_SOL:
        return "DUST"
    if report.harsh_pct > 0:
        return "PROFITABLE"
    if report.realized_pct > 0:
        return "PROFITABLE_IF_BAGS_IGNORED"
    return "LOSING"


async def main() -> None:
    path = Path(sys.argv[1] if len(sys.argv) > 1 else "wallets.txt")
    lookback = int(sys.argv[2] if len(sys.argv) > 2 else 60)
    wallets = load_wallets(path)
    print(f"grading {len(wallets)} wallets over {lookback} days\n", flush=True)

    client = HeliusSwaps()
    rows = []
    try:
        for i, (address, label) in enumerate(wallets, 1):
            swaps = await client.swaps(address, lookback)
            report = build_report(address, swaps)
            rows.append((label, address, report, swaps))
            print(
                f"  [{i}/{len(wallets)}] {label:14s} swaps={len(swaps):5d} "
                f"tokens={len(report.tokens):4d} closed={len(report.closed):4d}",
                flush=True,
            )
    finally:
        await client.close()

    print(
        f"\nskipped: {client.skipped_stable} stablecoin-quoted, "
        f"{client.skipped_unparsed} unparsed"
    )
    print(f"parsed by path: {client.by_path}")
    print(f"first transaction shape: {client.shape_sample}")
    sizes = sorted(abs(s.sol_amount) for _l, _a, _r, sw in rows for s in sw)
    if sizes:
        print(
            f"swap size SOL: min {sizes[0]:.4f} "
            f"p25 {sizes[len(sizes)//4]:.4f} median {sizes[len(sizes)//2]:.4f} "
            f"p75 {sizes[3*len(sizes)//4]:.4f} max {sizes[-1]:.4f}"
        )

    print("\n=== DID THEY MAKE MONEY? ===")
    print(
        f"{'wallet':14s} {'closed':>7} {'buys/day':>9} {'med buy':>9} "
        f"{'win%':>6} {'realized%':>10} {'harsh%':>9}  verdict"
    )
    for label, _addr, r, _s in sorted(rows, key=lambda x: -x[2].harsh_pct):
        print(
            f"{label:14s} {len(r.closed):7d} {r.buys_per_day:9.1f} "
            f"{r.median_buy_sol:9.3f} {r.win_rate_pct:6.1f} "
            f"{r.realized_pct:+10.1f} {r.harsh_pct:+9.1f}  {verdict(r)}"
        )

    tradeable = [row for row in rows if verdict(row[2]) == "PROFITABLE"]
    print(f"\nwallets profitable on their own money: {len(tradeable)} of {len(rows)}")

    print("\n=== DOES IT PERSIST? ===")
    boundary = int(time.time()) - (lookback // 2) * 86400
    pairs = []
    for label, _addr, _r, swaps in rows:
        early_swaps, late_swaps = split_by_time(swaps, boundary)
        early, late = build_report(label, early_swaps), build_report(label, late_swaps)
        if len(early.closed) < MIN_CLOSED_POSITIONS or len(late.closed) < MIN_CLOSED_POSITIONS:
            continue
        pairs.append((label, early, late))

    if len(pairs) < 4:
        print(f"only {len(pairs)} wallets have enough closed trades in both halves; "
              "no persistence claim can be made from this sample")
        return

    print(f"{'wallet':14s} {'first half':>12} {'second half':>13}")
    for label, early, late in sorted(pairs, key=lambda p: -p[1].harsh_pct):
        print(f"{label:14s} {early.harsh_pct:+12.1f} {late.harsh_pct:+13.1f}")

    winners = [p for p in pairs if p[1].harsh_pct > 0]
    losers = [p for p in pairs if p[1].harsh_pct <= 0]
    print(f"\nfirst-half winners (n={len(winners)}): "
          f"second-half mean {statistics.fmean(p[2].harsh_pct for p in winners):+.1f}%"
          if winners else "\nno first-half winners")
    if losers:
        print(f"first-half losers  (n={len(losers)}): "
              f"second-half mean {statistics.fmean(p[2].harsh_pct for p in losers):+.1f}%")

    if winners and losers:
        gap = (statistics.fmean(p[2].harsh_pct for p in winners)
               - statistics.fmean(p[2].harsh_pct for p in losers))
        print(f"\npersistence gap: {gap:+.1f} points")
        if gap > 0 and statistics.fmean(p[2].harsh_pct for p in winners) > 0:
            print("VERDICT: past profit predicts future profit here. "
                  "Worth sourcing a larger wallet universe and testing copyability.")
        else:
            print("VERDICT: past profit does not carry forward. There is nobody to "
                  "copy, and no exit rule fixes that.")


if __name__ == "__main__":
    asyncio.run(main())
