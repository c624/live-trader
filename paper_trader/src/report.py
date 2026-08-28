"""The decision table: aggregate the paper ledger into the numbers that pick
a strategy. Offline, reads ledger.csv, prints markdown.

Usage: python -m src.report [path/to/ledger.csv]
"""

from __future__ import annotations

import csv
import math
import sys
from collections import defaultdict

# hit_2x_within_72h encodes a hit as +100.0 and a miss as -0.01.
HIT_STRATEGY = "hit_2x_within_72h"


def load(path: str) -> list[dict]:
    with open(path) as fh:
        return [row for row in csv.DictReader(fh) if row.get("net_return_pct")]


def mean_ci(values: list[float]) -> tuple[float, float, float]:
    """(mean, lo95, hi95) with a normal approximation; wide on tiny n."""
    n = len(values)
    m = sum(values) / n
    if n < 2:
        return m, float("-inf"), float("inf")
    var = sum((v - m) ** 2 for v in values) / (n - 1)
    half = 1.96 * math.sqrt(var / n)
    return m, m - half, m + half


def wilson(hits: int, n: int) -> tuple[float, float, float]:
    """(rate, lo95, hi95) for a proportion; stable at small n."""
    if n == 0:
        return 0.0, 0.0, 0.0
    z = 1.96
    p = hits / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return p, center - half, center + half


def exit_table(rows: list[dict]) -> str:
    cells: dict[tuple[str, str], list[float]] = defaultdict(list)
    for row in rows:
        strategy = row["strategy"]
        if strategy == HIT_STRATEGY or strategy.startswith(("max_gain", "moonbag")):
            continue
        cells[(row["group"], strategy)].append(float(row["net_return_pct"]))
    lines = [
        "| group | strategy | n | mean % | 95% CI | median % | win % |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for (group, strategy), values in sorted(
        cells.items(), key=lambda kv: -mean_ci(kv[1])[0]
    ):
        m, lo, hi = mean_ci(values)
        values_sorted = sorted(values)
        median = values_sorted[len(values) // 2]
        win = 100 * sum(1 for v in values if v > 0) / len(values)
        ci = f"{lo:+.1f} to {hi:+.1f}" if math.isfinite(lo) else "n too small"
        lines.append(
            f"| {group} | {strategy} | {len(values)} | {m:+.1f} | {ci} "
            f"| {median:+.1f} | {win:.0f} |"
        )
    return "\n".join(lines)


def hit_table(rows: list[dict]) -> str:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for row in rows:
        if row["strategy"] != HIT_STRATEGY:
            continue
        entry = counts[row["group"]]
        entry[1] += 1
        if float(row["net_return_pct"]) > 0:
            entry[0] += 1
    lines = [
        "| group | n | 2x hits | hit rate | 95% CI |",
        "| --- | --- | --- | --- | --- |",
    ]
    for group, (hits, n) in sorted(counts.items(), key=lambda kv: -wilson(*kv[1])[0]):
        p, lo, hi = wilson(hits, n)
        lines.append(
            f"| {group} | {n} | {hits} | {100 * p:.0f}% | {100 * lo:.0f}% to {100 * hi:.0f}% |"
        )
    return "\n".join(lines)


def main() -> None:
    path = sys.argv[1] if len(sys.argv) > 1 else "state/ledger.csv"
    rows = load(path)
    positions = {(r["token"], r["entry_ts"]) for r in rows}
    print(f"Ledger rows: {len(rows)}, distinct scored positions: {len(positions)}\n")
    print("## Exit grid (net of fees, stop-first on ambiguity)\n")
    print(exit_table(rows))
    print("\n## Doubled within 72h (the fat-tail proportion)\n")
    print(hit_table(rows))


if __name__ == "__main__":
    main()
