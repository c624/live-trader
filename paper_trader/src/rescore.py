"""Re-score the lab's positions with the corrected engine and print both.

Reads the positions the paper trader has opened, re-fetches their candles,
and scores each one twice: once the way the original engine did and once
with realistic stop fills and rugs counted. The difference between the two
edges is the size of the error the live pilot exposed.

Usage: python -m src.rescore [state_dir] [group] [limit]
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from .engine import Candles, simulate_exit
from .gecko import Gecko
from .honest_engine import simulate_exit_honest, summarize

TP, SL, HOLD, FEES = 100, 50, 24, 3.2
MATURITY_SECONDS = (HOLD + 2) * 3600


def load_positions(state_dir: Path, group: str) -> list[dict]:
    """Every position the lab has opened in this group, matured or riding."""
    state = json.loads((state_dir / "paper_state.json").read_text())
    out = []
    for bucket in ("riding", "open"):
        for p in state.get(bucket, []):
            if group in (p.get("groups") or []) or p.get("group") == group:
                out.append(p)
    return out


async def main() -> None:
    state_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "state")
    group = sys.argv[2] if len(sys.argv) > 2 else "brand_new"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 400

    positions = load_positions(state_dir, group)[:limit]
    print(f"re-scoring {len(positions)} {group} positions\n")

    gecko = Gecko()
    original, honest = [], []
    dropped_by_original = 0
    try:
        for i, position in enumerate(positions, 1):
            rows = await gecko.candles(
                position["pool"], position["entry_ts"] + MATURITY_SECONDS
            )
            candles = Candles(rows=rows)
            entry_ts = position["entry_ts"]

            old = simulate_exit(
                candles, entry_ts, take_profit_pct=TP, stop_loss_pct=SL,
                hold_hours=HOLD, round_trip_fee_pct=FEES,
            )
            if old is None:
                dropped_by_original += 1
            else:
                original.append(old["net_return_pct"])

            honest.append(
                simulate_exit_honest(
                    candles, entry_ts, take_profit_pct=TP, stop_loss_pct=SL,
                    hold_hours=HOLD, round_trip_fee_pct=FEES,
                )
            )
            if i % 25 == 0:
                print(f"  ...{i}/{len(positions)}")
    finally:
        await gecko.close()

    print("\n=== ORIGINAL ENGINE (as the strategy was approved) ===")
    print(f"scored {len(original)}, dropped as unscoreable {dropped_by_original}")
    if original:
        print(f"edge: {sum(original)/len(original):+.2f}% per trade")

    print("\n=== CORRECTED ENGINE ===")
    summary = summarize(honest)
    print(f"n={summary['n']} (nothing dropped)")
    for row in summary["rows"]:
        print(
            f"  {row['reason']:16s} n={row['n']:4d} ({row['share_pct']:5.1f}%)"
            f"  mean {row['mean_pct']:+8.2f}%  -> {row['contribution_pct']:+7.2f} pts"
        )
    print(f"\ncorrected edge: {summary['edge_pct']:+.2f}% per trade")

    if original:
        gap = summary["edge_pct"] - sum(original) / len(original)
        print(f"difference vs the approved number: {gap:+.2f} points")


if __name__ == "__main__":
    asyncio.run(main())
