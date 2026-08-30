"""Can a rug be seen before the buy?

The corrected engine says 58.7% of the pools this strategy bought went to
zero, which is why the live pilot lost money. That leaves exactly one
question worth answering before any more money moves: does anything
observable at entry separate the rugs from the survivors?

The study is built to fail honestly rather than to find something:

  * features look strictly backwards from the entry bar, so any rule found
    here is one the live bot could actually have run
  * the sample is split by time; rules are searched on the older half and
    scored on the newer half, which the search never sees
  * the holdout number is reported with a bootstrap interval and with the
    count of rules tried, because searching hard enough always finds a rule

Usage: python -m src.rugstudy [state_dir] [group|all] [limit]
"""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import sys
from pathlib import Path

from .engine import Candles
from .features import FEATURE_NAMES, entry_features
from .gecko import Gecko
from .honest_engine import simulate_exit_honest

TP, SL, HOLD, FEES = 100, 50, 24, 3.2
MATURITY_SECONDS = (HOLD + 2) * 3600
RUG_REASONS = {"rug_no_data", "rug_went_quiet"}
MIN_KEPT = 25          # a rule that keeps fewer trades than this is noise
BOOTSTRAP_ROUNDS = 2000


def load_positions(state_dir: Path, group: str) -> list[dict]:
    state = json.loads((state_dir / "paper_state.json").read_text())
    out = []
    for bucket in ("riding", "open"):
        for p in state.get(bucket, []):
            if group == "all" or group in (p.get("groups") or []) or p.get("group") == group:
                out.append(p)
    out.sort(key=lambda p: p["entry_ts"])
    return out


async def collect(state_dir: Path, group: str, limit: int) -> list[dict]:
    """One row per position: entry features plus the corrected outcome."""
    positions = load_positions(state_dir, group)[:limit]
    print(f"studying {len(positions)} {group} positions\n", flush=True)

    gecko = Gecko()
    rows: list[dict] = []
    try:
        for i, position in enumerate(positions, 1):
            entry_ts = position["entry_ts"]
            raw = await gecko.candles_with_volume(
                position["pool"], entry_ts + MATURITY_SECONDS
            )
            outcome = simulate_exit_honest(
                Candles(rows=[r[:5] for r in raw]),
                entry_ts,
                take_profit_pct=TP,
                stop_loss_pct=SL,
                hold_hours=HOLD,
                round_trip_fee_pct=FEES,
            )
            record = {
                "symbol": position.get("symbol", "?"),
                "entry_ts": entry_ts,
                "reason": outcome.exit_reason,
                "net_pct": outcome.net_return_pct,
                "is_rug": outcome.exit_reason in RUG_REASONS,
            }
            record.update(entry_features(raw, entry_ts, position.get("reserve_usd")))
            rows.append(record)
            if i % 25 == 0:
                print(f"  ...{i}/{len(positions)}", flush=True)
    finally:
        await gecko.close()
    return rows


def edge(rows: list[dict]) -> float:
    return statistics.fmean(r["net_pct"] for r in rows) if rows else 0.0


def rug_rate(rows: list[dict]) -> float:
    return 100 * sum(r["is_rug"] for r in rows) / len(rows) if rows else 0.0


def quantiles(values: list[float], cuts: int) -> list[float]:
    ordered = sorted(values)
    return [
        ordered[min(len(ordered) - 1, int(len(ordered) * k / cuts))]
        for k in range(1, cuts)
    ]


def describe_buckets(rows: list[dict], feature: str, cuts: int = 4) -> None:
    values = [r[feature] for r in rows if r[feature] is not None]
    missing = [r for r in rows if r[feature] is None]
    if len(values) < cuts * 5:
        print(f"  {feature}: too few values ({len(values)})")
        return
    bounds = [float("-inf")] + quantiles(values, cuts) + [float("inf")]
    print(f"  {feature}:")
    for i in range(cuts):
        low, high = bounds[i], bounds[i + 1]
        bucket = [
            r for r in rows
            if r[feature] is not None and low <= r[feature] < high
        ]
        if not bucket:
            continue
        print(
            f"    [{low:>10.4g} , {high:>10.4g})  n={len(bucket):4d}"
            f"  rug {rug_rate(bucket):5.1f}%  edge {edge(bucket):+8.2f}%"
        )
    if missing:
        print(
            f"    {'missing':>25}  n={len(missing):4d}"
            f"  rug {rug_rate(missing):5.1f}%  edge {edge(missing):+8.2f}%"
        )


def apply_rule(rows: list[dict], feature: str, op: str, threshold: float) -> list[dict]:
    """Rows the rule would have bought. Missing feature never qualifies."""
    kept = []
    for r in rows:
        v = r[feature]
        if v is None:
            continue
        if (op == ">=" and v >= threshold) or (op == "<=" and v <= threshold):
            kept.append(r)
    return kept


def search_rules(discovery: list[dict]) -> tuple[list[dict], int]:
    """Every single-threshold rule, best discovery edge first."""
    results, tried = [], 0
    for feature in FEATURE_NAMES:
        values = [r[feature] for r in discovery if r[feature] is not None]
        if len(values) < MIN_KEPT * 2:
            continue
        for threshold in sorted(set(quantiles(values, 10))):
            for op in (">=", "<="):
                tried += 1
                kept = apply_rule(discovery, feature, op, threshold)
                if len(kept) < MIN_KEPT:
                    continue
                results.append(
                    {
                        "feature": feature,
                        "op": op,
                        "threshold": threshold,
                        "n": len(kept),
                        "edge": edge(kept),
                        "rug": rug_rate(kept),
                    }
                )
    results.sort(key=lambda r: -r["edge"])
    return results, tried


def bootstrap_ci(rows: list[dict], rounds: int = BOOTSTRAP_ROUNDS) -> tuple[float, float]:
    if len(rows) < 5:
        return (float("nan"), float("nan"))
    rng = random.Random(20260830)
    values = [r["net_pct"] for r in rows]
    means = sorted(
        statistics.fmean(rng.choices(values, k=len(values))) for _ in range(rounds)
    )
    return means[int(0.05 * rounds)], means[int(0.95 * rounds)]


def report(rows: list[dict]) -> None:
    print("\n=== POPULATION (corrected engine) ===")
    print(f"n={len(rows)}  rug rate {rug_rate(rows):.1f}%  edge {edge(rows):+.2f}%")

    print("\n=== RUG RATE AND EDGE BY ENTRY FEATURE ===")
    for feature in FEATURE_NAMES:
        describe_buckets(rows, feature)

    half = len(rows) // 2
    discovery, holdout = rows[:half], rows[half:]
    print(
        f"\n=== SPLIT SAMPLE ===\ndiscovery n={len(discovery)} "
        f"(older half), holdout n={len(holdout)} (newer half, unseen by the search)"
    )
    print(
        f"holdout baseline, no filter: n={len(holdout)}"
        f"  rug {rug_rate(holdout):5.1f}%  edge {edge(holdout):+.2f}%"
    )

    ranked, tried = search_rules(discovery)
    if not ranked:
        print("no rule kept enough trades to test")
        return

    print(f"\n{tried} rules tried on the discovery half. Top 5 by discovery edge:")
    for rule in ranked[:5]:
        kept = apply_rule(holdout, rule["feature"], rule["op"], rule["threshold"])
        lo, hi = bootstrap_ci(kept)
        print(
            f"  {rule['feature']:18s} {rule['op']} {rule['threshold']:<12.4g}"
            f" discovery: n={rule['n']:3d} rug {rule['rug']:5.1f}% edge {rule['edge']:+8.2f}%"
            f"  ->  HOLDOUT: n={len(kept):3d} rug {rug_rate(kept):5.1f}%"
            f" edge {edge(kept):+8.2f}%  [90% CI {lo:+.1f} .. {hi:+.1f}]"
        )

    best = ranked[0]
    kept = apply_rule(holdout, best["feature"], best["op"], best["threshold"])
    lo, hi = bootstrap_ci(kept)
    print("\n=== VERDICT ===")
    print(
        f"best discovery rule: {best['feature']} {best['op']} {best['threshold']:.4g}"
    )
    print(f"on the holdout it would have taken {len(kept)} of {len(holdout)} trades")
    print(f"holdout edge {edge(kept):+.2f}% per trade, 90% CI {lo:+.1f} .. {hi:+.1f}")
    if lo > 0:
        print("VERDICT: holdout edge is positive with the interval clear of zero.")
    else:
        print(
            "VERDICT: NOT tradeable. The interval includes zero or is negative, "
            "so this rule is not distinguishable from luck."
        )


def dump_csv(rows: list[dict]) -> None:
    """Printed so the sample can be re-analysed without re-fetching candles."""
    header = ["symbol", "entry_ts", "reason", "net_pct", "is_rug"] + FEATURE_NAMES
    print("\n=== DATA ===")
    print("DATA," + ",".join(header))
    for r in rows:
        print("DATA," + ",".join("" if r[k] is None else str(r[k]) for k in header))


async def main() -> None:
    state_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "state")
    group = sys.argv[2] if len(sys.argv) > 2 else "all"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 400
    rows = await collect(state_dir, group, limit)
    if not rows:
        print("no positions")
        return
    report(rows)
    dump_csv(rows)


if __name__ == "__main__":
    asyncio.run(main())
