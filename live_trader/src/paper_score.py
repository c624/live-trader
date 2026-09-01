"""Does the paper record show an edge, or just noise?

Per-trade returns here are enormously dispersed -- mostly near a total loss,
occasionally a multiple -- so a mean on its own says very little. Our own
recorded run (n=188, mean +9.2%, 95% CI +2.0 to +16.5) implies a per-trade
standard deviation around 50 points. At that spread the fifteen trades a $30
pilot buys carry a standard error near 13 points, which cannot tell +8.7% from
zero. So this reports the interval, not the mean alone, and says outright when
there are too few trades to conclude anything.

Usage: python -m live_trader.src.paper_score [state_dir]
"""

from __future__ import annotations

import csv
import math
import statistics
import sys
from pathlib import Path

EDGE_HYPOTHESIS = 8.7      # the modelled per-trade edge, in percent


def read_closed(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path) as fh:
        return [r for r in csv.DictReader(fh) if r.get("action") == "paper_sell"]


def pct_returns(rows: list[dict], ticket: float) -> list[float]:
    out = []
    for row in rows:
        try:
            proceeds = float(row["usd_value"])
        except (TypeError, ValueError):
            continue
        out.append((proceeds - ticket) / ticket * 100)
    return out


def needed_for(std: float, edge: float = EDGE_HYPOTHESIS) -> int:
    """Trades needed before a two-standard-error interval clears zero."""
    if edge <= 0:
        return 0
    return int(math.ceil((2 * std / edge) ** 2))


def report(returns: list[float], ticket: float) -> str:
    n = len(returns)
    if n == 0:
        return "No closed paper trades yet."
    mean = statistics.fmean(returns)
    lines = [f"closed trades   {n}",
             f"mean            {mean:+.2f}% per trade",
             f"median          {statistics.median(returns):+.2f}%",
             f"win rate        {100 * sum(1 for r in returns if r > 0) / n:.0f}%",
             f"total P&L       ${sum(returns) * ticket / 100:+.2f} on ${n * ticket:.0f} staked"]
    if n < 2:
        lines.append("\nOne trade is an anecdote. No interval can be computed.")
        return "\n".join(lines)

    std = statistics.stdev(returns)
    stderr = std / math.sqrt(n)
    low, high = mean - 2 * stderr, mean + 2 * stderr
    lines += [f"spread          {std:.1f} points per trade",
              f"95% interval    {low:+.2f}% to {high:+.2f}%"]

    need = needed_for(std)
    if low > 0:
        lines.append(f"\nVERDICT: profitable. The interval clears zero on {n} trades.")
    elif high < 0:
        lines.append(f"\nVERDICT: losing. The interval is below zero on {n} trades.")
    else:
        lines.append(
            f"\nVERDICT: inconclusive -- the interval spans zero. At this spread "
            f"it takes about {need} trades to separate a {EDGE_HYPOTHESIS}% edge "
            f"from nothing; there are {n}. Anything read from the mean alone "
            f"right now is noise.")
    return "\n".join(lines)


def by_arm(rows: list[dict]) -> dict:
    """Group closed trades by the arm that took them."""
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(row.get("arm") or "(unlabelled)", []).append(row)
    return groups


def leaderboard(groups: dict, ticket: float) -> str:
    """One line per arm, so arms are compared rather than admired.

    Sorted by the lower bound of the interval, not the mean: an arm with a
    high mean on six trades is not beating an arm with a modest mean on two
    hundred, and sorting by mean would put it on top every time.
    """
    lines = [f"{'arm':<12} {'n':>4} {'mean':>9} {'median':>9} {'win':>5} "
             f"{'95% interval':>22}  verdict"]
    scored = []
    for name, rows in groups.items():
        returns = pct_returns(rows, ticket)
        if not returns:
            continue
        mean = statistics.fmean(returns)
        if len(returns) > 1:
            stderr = statistics.stdev(returns) / math.sqrt(len(returns))
            low, high = mean - 2 * stderr, mean + 2 * stderr
        else:
            low, high = float("-inf"), float("inf")
        scored.append((low, name, returns, mean, high))
    for low, name, returns, mean, high in sorted(scored, reverse=True):
        n = len(returns)
        win = 100 * sum(1 for r in returns if r > 0) / n
        if low > 0:
            verdict = "PROFITABLE"
        elif high < 0:
            verdict = "losing"
        else:
            verdict = "inconclusive"
        span = ("     n too small" if n < 2
                else f"{low:+8.1f}% to {high:+8.1f}%")
        lines.append(f"{name:<12} {n:>4} {mean:>+8.2f}% "
                     f"{statistics.median(returns):>+8.2f}% {win:>4.0f}% "
                     f"{span:>22}  {verdict}")
    return "\n".join(lines)


def main() -> None:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("state/paper")
    ticket = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
    rows = read_closed(base / "trades.csv")
    print(f"=== PAPER RESULT ({base}) ===")
    groups = by_arm(rows)
    if len(groups) > 1 or (groups and "(unlabelled)" not in groups):
        print(leaderboard(groups, ticket))
        print()
    print(report(pct_returns(rows, ticket), ticket))


if __name__ == "__main__":
    main()
