"""Sweep exit policies over the lab's positions on honest terms.

The entry-filter study found no screen that turns this population positive,
and showed why: even where rugs are nearly absent the strategy loses,
because winners are capped at +100% while losers fill at -60% or worse. So
the question moves to the exit. This scores every position under a grid of
take-profit / stop / trailing-stop / hold combinations from a single pass
over the candle data.

The grid is large, which means the best cell is partly luck. The same
defence as the entry study applies: policies are ranked on the older half of
the sample and the winner is reported on the newer half, with a bootstrap
interval and the number of cells tried.

Usage: python -m src.sweep [state_dir] [group|all] [limit]
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import sys
from pathlib import Path

from .engine import Candles
from .gecko import Gecko
from .policy_engine import Policy, simulate_policy

FEES = 3.2
MAX_HOLD_HOURS = 24
MATURITY_SECONDS = (MAX_HOLD_HOURS + 2) * 3600
BOOTSTRAP_ROUNDS = 2000

TAKE_PROFITS: list[float | None] = [50, 100, 200, 400, None]
STOPS: list[float | None] = [30, 50, 80, None]
TRAILS: list[float | None] = [None, 30, 50]
HOLDS = [2, 6, 12, 24]

# The wallets that actually make money hold for 301 seconds and take small
# gains, so the short grid needs targets and stops sized for minutes rather
# than for a 24-hour lottery ticket.
SHORT_TAKE_PROFITS: list[float | None] = [10, 20, 30, 50, 100, None]
SHORT_STOPS: list[float | None] = [10, 20, 30, 50, None]
SHORT_TRAILS: list[float | None] = [None, 10, 20]
SHORT_HOLDS = [2 / 60, 5 / 60, 10 / 60, 15 / 60, 30 / 60, 1.0]


def build_grid(short: bool = False) -> list[Policy]:
    """Every combination, minus the ones that are the same rule twice."""
    holds = SHORT_HOLDS if short else HOLDS
    take_profits = SHORT_TAKE_PROFITS if short else TAKE_PROFITS
    stops = SHORT_STOPS if short else STOPS
    trails = SHORT_TRAILS if short else TRAILS
    grid = []
    for hold in holds:
        for tp in take_profits:
            for sl in stops:
                for trail in trails:
                    # With no stop of any kind and no target, hold time is the
                    # only rule; that cell is worth exactly one entry.
                    grid.append(
                        Policy(
                            take_profit_pct=tp,
                            stop_loss_pct=sl,
                            trail_pct=trail,
                            hold_hours=hold,
                            round_trip_fee_pct=FEES,
                        )
                    )
    return grid


def load_positions(state_dir: Path, group: str) -> list[dict]:
    state = json.loads((state_dir / "paper_state.json").read_text())
    out = []
    for bucket in ("riding", "open"):
        for p in state.get(bucket, []):
            if group == "all" or group in (p.get("groups") or []) or p.get("group") == group:
                out.append(p)
    out.sort(key=lambda p: p["entry_ts"])
    return out


async def score_all(state_dir: Path, group: str, limit: int, grid: list[Policy],
                    short: bool = False, at_birth: bool = False):
    """Returns (positions, results) where results[i][j] is position i under policy j."""
    positions = load_positions(state_dir, group)[:limit]
    print(f"sweeping {len(grid)} policies over {len(positions)} {group} positions\n", flush=True)

    gecko = Gecko()
    results: list[list] = []
    kept: list[dict] = []
    try:
        for i, position in enumerate(positions, 1):
            entry_ts = position["entry_ts"]
            # A five-minute hold cannot be scored on five-minute bars.
            rows = (
                await gecko.candles_fine(position["pool"], entry_ts + 16 * 3600)
                if short
                else await gecko.candles(position["pool"], entry_ts + MATURITY_SECONDS)
            )
            candles = Candles(rows=rows)
            if at_birth:
                # Every sweep so far entered where the lab detected the pool,
                # which is minutes to an hour after it opened. The wallets
                # that make money enter at second zero, so this measures the
                # same pools bought at their first printed bar: the best case
                # perfect speed could ever deliver.
                if not rows:
                    continue
                entry_ts = rows[0][0]
            results.append([simulate_policy(candles, entry_ts, p) for p in grid])
            kept.append(position)
            if i % 25 == 0:
                print(f"  ...{i}/{len(positions)}", flush=True)
    finally:
        await gecko.close()
    return kept, results


def edge_of(results: list[list], indices: list[int], j: int) -> float:
    return statistics.fmean(results[i][j].net_return_pct for i in indices)


def bootstrap_ci(values: list[float], rounds: int = BOOTSTRAP_ROUNDS):
    if len(values) < 5:
        return (float("nan"), float("nan"))
    rng = random.Random(20260830)
    means = sorted(
        statistics.fmean(rng.choices(values, k=len(values))) for _ in range(rounds)
    )
    return means[int(0.05 * rounds)], means[int(0.95 * rounds)]


def trimmed_mean(values: list[float], frac: float = 0.05) -> float:
    """Mean with the extreme tails dropped.

    The first live run of this sweep ranked policies by plain mean and put a
    single position worth tens of millions of percent at the top of every
    cell that held it. A number like that is a broken price series, not a
    trade anybody could make, and ranking on it measures nothing.
    """
    if len(values) < 10:
        return statistics.fmean(values) if values else 0.0
    ordered = sorted(values)
    cut = max(1, int(len(ordered) * frac))
    return statistics.fmean(ordered[cut:-cut])


def robustness(values: list[float]) -> tuple[float, float]:
    """Median, and the mean with the single best trade removed.

    A policy with no take-profit can post a fine average off one lottery
    ticket. If dropping the best trade kills the edge, it is not a strategy,
    it is that one trade.
    """
    if len(values) < 3:
        return (float("nan"), float("nan"))
    without_best = sorted(values)[:-1]
    return statistics.median(values), statistics.fmean(without_best)


def breakdown(results: list[list], indices: list[int], j: int) -> str:
    counts: dict[str, int] = {}
    for i in indices:
        reason = results[i][j].exit_reason
        counts[reason] = counts.get(reason, 0) + 1
    total = len(indices)
    parts = [
        f"{reason} {100 * n / total:.0f}%"
        for reason, n in sorted(counts.items(), key=lambda kv: -kv[1])
    ]
    return ", ".join(parts)


def report(grid: list[Policy], results: list[list], positions: list[dict]) -> None:
    n = len(results)
    half = n // 2
    discovery = list(range(half))
    holdout = list(range(half, n))

    baseline = next(
        (j for j, p in enumerate(grid)
         if p.take_profit_pct == 100 and p.stop_loss_pct == 50
         and p.trail_pct is None and p.hold_hours == 24),
        0,
    )
    print("\n=== THE POLICY WE TRADED LIVE ===")
    print(f"{grid[baseline].label}: whole sample edge {edge_of(results, list(range(n)), baseline):+.2f}%")
    print(f"  exits: {breakdown(results, list(range(n)), baseline)}")

    ranked = sorted(
        range(len(grid)),
        key=lambda j: -trimmed_mean(
            [results[i][j].net_return_pct for i in discovery]
        ),
    )
    print(
        f"\n=== SPLIT SAMPLE ===\ndiscovery n={len(discovery)} (older), "
        f"holdout n={len(holdout)} (newer, never seen by the ranking)"
    )
    print(f"{len(grid)} policies ranked on the discovery half. Top 12:")
    print("(disc and hold are 5% trimmed means; mean is the raw average)")
    print(
        f"  {'policy':22s} {'disc':>9} {'hold':>9} {'mean':>11}"
        f" {'median':>8} {'ex-best':>10}"
    )
    for j in ranked[:12]:
        values = [results[i][j].net_return_pct for i in holdout]
        median, ex_best = robustness(values)
        print(
            f"  {grid[j].label:22s}"
            f" {trimmed_mean([results[i][j].net_return_pct for i in discovery]):+9.2f}"
            f" {trimmed_mean(values):+9.2f} {statistics.fmean(values):+11.2f}"
            f" {median:+8.2f} {ex_best:+10.2f}"
        )

    best = ranked[0]
    values = [results[i][best].net_return_pct for i in holdout]
    lo, hi = bootstrap_ci(values)
    print("\n=== VERDICT ===")
    print(f"best discovery policy: {grid[best].label}")
    median, ex_best = robustness(values)
    print(f"holdout edge {statistics.fmean(values):+.2f}% per trade, 90% CI {lo:+.1f} .. {hi:+.1f}")
    print(f"holdout median {median:+.2f}%, edge without the single best trade {ex_best:+.2f}%")
    print(f"  exits: {breakdown(results, holdout, best)}")
    if lo > 0 and ex_best > 0 and trimmed_mean(values) > 0:
        print("VERDICT: positive on unseen data, interval clear of zero, "
              "and it survives losing its best trade.")
    elif lo > 0:
        print("VERDICT: FRAGILE. Positive overall but the edge dies without "
              "its single best trade; that is one lottery ticket, not an edge.")
    else:
        print("VERDICT: NOT tradeable. The interval includes zero or is negative.")

    print("\n=== LARGEST SINGLE-POSITION RETURNS (sanity check) ===")
    print("a real memecoin can 10x; a five-figure percentage is a broken price series")
    ride = next(
        (j for j, p in enumerate(grid)
         if p.take_profit_pct is None and p.stop_loss_pct is None
         and p.trail_pct is None),
        0,
    )
    extremes = sorted(
        range(len(results)), key=lambda i: -results[i][ride].net_return_pct
    )[:8]
    for i in extremes:
        position = positions[i] if i < len(positions) else {}
        print(
            f"  {str(position.get('symbol', '?'))[:18]:20s}"
            f" {results[i][ride].net_return_pct:+15.1f}%"
            f"  entry_price={position.get('entry_price_api')}"
        )

    # A policy that only wins in one half is a coin flip; show the honest pair.
    both = [
        j for j in ranked[:40]
        if trimmed_mean([results[i][j].net_return_pct for i in discovery]) > 0
        and trimmed_mean([results[i][j].net_return_pct for i in holdout]) > 0
    ]
    print(f"\npolicies positive in BOTH halves: {len(both)} of {len(grid)}")
    for j in both[:10]:
        print(
            f"  {grid[j].label:22s} disc {trimmed_mean([results[i][j].net_return_pct for i in discovery]):+8.2f}"
            f"  hold {trimmed_mean([results[i][j].net_return_pct for i in holdout]):+8.2f}"
        )


async def main() -> None:
    state_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "state")
    group = sys.argv[2] if len(sys.argv) > 2 else "all"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    mode = sys.argv[4].lower() if len(sys.argv) > 4 else "long"
    short = mode in ("short", "birth", "true", "1")
    at_birth = mode == "birth"
    grid = build_grid(short=short)
    if short:
        print("SHORT-HOLD MODE: one-minute bars, holds of 2 to 60 minutes")
    if at_birth:
        print("ENTRY AT POOL BIRTH: entering on the first printed bar, not at "
              "the lab's detection time. This is the ceiling perfect speed buys.")
    positions, results = await score_all(
        state_dir, group, limit, grid, short=short, at_birth=at_birth
    )
    if not results:
        print("no positions")
        return
    report(grid, results, positions)


if __name__ == "__main__":
    asyncio.run(main())
