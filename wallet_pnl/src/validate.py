"""Check the parser against a wallet whose trades are already known.

Four parser versions have now disagreed with reality, each time discovered
only after a full run produced a plausible-looking table. The bot's own
wallet is the fix for that: every buy was $15, every fill is in trades.csv,
so the right answer is known before the parser speaks. A parser that cannot
reproduce a ledger we wrote ourselves cannot be trusted on strangers.

Usage: python -m src.validate <wallet> [days]
"""

from __future__ import annotations

import asyncio
import sys

from .helius import HeliusSwaps
from .ledger import build_report


async def main() -> None:
    wallet = sys.argv[1]
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 7

    client = HeliusSwaps()
    try:
        swaps = await client.swaps(wallet, days)
    finally:
        await client.close()

    print(f"parsed {len(swaps)} swaps over {days} days")
    print(f"parsed by path: {client.by_path}")
    print(f"skipped: {client.skipped_stable} stable, {client.skipped_unparsed} unparsed")
    print(f"first transaction shape: {client.shape_sample}\n")

    print(f"{'when':>12}  {'mint':10} {'side':5} {'tokens':>18} {'SOL':>12}")
    for s in swaps:
        side = "buy" if s.token_amount > 0 else "sell"
        print(
            f"{s.ts:>12}  {s.mint[:10]:10} {side:5} "
            f"{s.token_amount:>18.4f} {s.sol_amount:>12.6f}"
        )

    report = build_report(wallet, swaps)
    print(
        f"\ntokens={len(report.tokens)} closed={len(report.closed)} "
        f"deployed={report.sol_deployed:.4f} SOL "
        f"realized={report.realized_sol:+.4f} SOL "
        f"median_buy={report.median_buy_sol:.4f} SOL"
    )


if __name__ == "__main__":
    asyncio.run(main())
