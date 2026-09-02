"""Do any of the recorded entry-time facts separate a winner from a loser?

Entry timing is settled: waiting removes rugs and nothing else, and past about
four minutes the result converges on the round-trip cost. So the open question
is which token to buy, and that is a question about features rather than about
clocks.

This joins what the chain said at entry to what the position actually did, and
splits each feature at its median. A feature that separates anything will show
a gap between its halves that survives its own confidence interval; one that
does not will show two numbers a rounding error apart, which is what the mint
and freeze authority flags did across every launch.

The split is by median rather than by a threshold chosen after looking, so the
comparison cannot be tuned into significance. Anything that looks promising
here still has to be confirmed by an arm that trades on it, because a feature
picked out of many by its own data is exactly how 780 configurations came to
look profitable before the engine was corrected.

Usage: python -m live_trader.src.feature_score <state_dir> [ticket_usd]
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from pathlib import Path

NUMERIC = ["tx_count", "tx_per_min", "tx_span_s", "price_impact_pct",
           "entry_lag_s", "top1_share", "holders_sampled",
           # Attention and momentum. The analyser predated these fields and
           # silently scored everything except the category under study.
           "boosts", "m5_buys", "m5_sells", "buy_ratio_m5", "h1_buys",
           "h1_sells", "vol_m5_usd", "vol_h1_usd", "chg_m5_pct", "chg_h1_pct",
           "liquidity_usd",
           "mentions_15m", "mentions_1h", "authors_1h", "reach_1h"]
BOOLEAN = ["mint_authority", "freeze_authority", "tx_capped"]


def load_features(path: Path) -> dict[str, dict]:
    """Entry-time facts, keyed by mint. First reading wins."""
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with open(path) as fh:
        for row in csv.DictReader(fh):
            out.setdefault(row["mint"], row)
    return out


def load_outcomes(path: Path) -> dict[str, float]:
    """Percentage return per mint, from closed paper positions."""
    if not path.exists():
        return {}
    state = json.loads(path.read_text())
    out = {}
    for p in state.get("positions", []):
        if p.get("status") != "closed" or "pnl_usd" not in p:
            continue
        cost = p.get("cost_usd") or 0
        if cost:
            out[p["mint"]] = p["pnl_usd"] / cost * 100
    return out


def _interval(values: list[float]) -> tuple[float, float, float]:
    mean = statistics.fmean(values)
    if len(values) < 2:
        return mean, float("-inf"), float("inf")
    stderr = statistics.stdev(values) / math.sqrt(len(values))
    return mean, mean - 2 * stderr, mean + 2 * stderr


def split_report(name: str, pairs: list[tuple[float, float]]) -> str | None:
    """Compare outcomes above and below the feature's median."""
    pairs = [(v, r) for v, r in pairs if v is not None]
    if len(pairs) < 20:
        return None
    pairs.sort()
    mid = len(pairs) // 2
    low = [r for _, r in pairs[:mid]]
    high = [r for _, r in pairs[mid:]]
    if not low or not high:
        return None

    lo_mean, lo_a, lo_b = _interval(low)
    hi_mean, hi_a, hi_b = _interval(high)
    gap = hi_mean - lo_mean
    # Two intervals that overlap have not distinguished anything, whatever
    # the gap between their means looks like.
    separates = "SEPARATES" if (hi_a > lo_b or lo_a > hi_b) else "-"
    return (f"{name:18} n={len(pairs):5d} median={pairs[mid][0]:>10.3f}  "
            f"low {lo_mean:+7.2f}%  high {hi_mean:+7.2f}%  "
            f"gap {gap:+7.2f}  {separates}")


def main() -> None:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("state/paper")
    features = load_features(base / "features.csv")
    outcomes = load_outcomes(base / "live_state.json")
    joined = [(features[m], r) for m, r in outcomes.items() if m in features]

    print(f"=== FEATURE vs OUTCOME ({base}) ===")
    print(f"entries with features {len(features)}, closed positions "
          f"{len(outcomes)}, joined {len(joined)}")
    if len(joined) < 20:
        print("\nToo few joined rows to say anything. Nothing is concluded.")
        return

    for name in NUMERIC:
        pairs = []
        for feat, ret in joined:
            raw = feat.get(name)
            if raw in (None, "", "None"):
                continue
            try:
                pairs.append((float(raw), ret))
            except ValueError:
                continue
        line = split_report(name, pairs)
        if line:
            print(line)
        else:
            seen = sum(1 for f, _ in joined
                       if f.get(name) not in (None, "", "None"))
            print(f"{name:18} only {seen} readings, not enough to split")

    for name in BOOLEAN:
        groups: dict[str, list[float]] = {}
        for feat, ret in joined:
            raw = feat.get(name)
            if raw in (None, "", "None"):
                continue
            groups.setdefault(raw, []).append(ret)
        if len(groups) < 2:
            only = next(iter(groups), "nothing")
            print(f"{name:18} constant ({only}) across every entry -- "
                  f"cannot separate anything")
            continue
        parts = " ".join(f"{k}: {statistics.fmean(v):+.2f}% (n={len(v)})"
                         for k, v in sorted(groups.items()))
        print(f"{name:18} {parts}")


if __name__ == "__main__":
    main()
