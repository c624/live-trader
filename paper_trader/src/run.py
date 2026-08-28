"""Forward paper trader: open hypothesis positions now, score them after 24h.

Groups, defined BEFORE the outcomes so hindsight cannot creep in:

  age_1_24h    pool is 1-24 hours old (the least-bad backtest cell)
  liq_pocket   liquidity between $50k and $250k (the 8-for-8 cell to retest)
  control      any trending pool with >= $10k liquidity (the baseline every
               hypothesis must beat, or it is noise)

State lives in a JSON file plus an append-only CSV ledger, both committed to
a dedicated branch by the workflow. Every position is scored through the same
exit grid; nothing is ever dropped or retro-edited.
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

from .engine import EXIT_GRID, Candles, max_gain_pct, simulate_exit, simulate_moonbag

HOLD_HOURS = 24
RIDE_HOURS = 72
FEES_PCT = 3.2
MATURITY_SECONDS = (HOLD_HOURS + 2) * 3600
RIDE_MATURITY_SECONDS = (RIDE_HOURS + 3) * 3600
MAX_NEW_PER_GROUP = 30
SEEN_TTL_SECONDS = 7 * 86400


def parse_created(created: str | None) -> float | None:
    if not created:
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def classify(row: dict, now: float) -> list[str]:
    """Which groups a pool row qualifies for. A row may sit in several."""
    groups = []
    reserve = row.get("reserve_usd")
    created = parse_created(row.get("pool_created_at"))
    age_hours = (now - created) / 3600 if created else None
    if reserve is None or row.get("price_usd") is None or not row.get("pool"):
        return groups
    if reserve >= 10_000:
        groups.append("control")
    # Like-for-like baseline: trending pools in the same size class as the
    # hypothesis groups. Plain "control" includes blue chips whose steadiness
    # would flatter any memecoin hypothesis it is compared against.
    if 10_000 <= reserve <= 500_000:
        groups.append("control_small")
    if age_hours is not None and 1 <= age_hours <= 24 and reserve >= 10_000:
        groups.append("age_1_24h")
    if 50_000 <= reserve <= 250_000:
        groups.append("liq_pocket")
    if age_hours is not None and age_hours <= 1 and reserve >= 5_000:
        groups.append("brand_new")
    volume = row.get("volume_h1_usd")
    if volume is not None and volume >= 50_000 and volume >= reserve:
        groups.append("vol_surge")
    return groups


def open_positions(rows: list[dict], state: dict, now: float) -> list[dict]:
    """Create new paper positions, deduped against everything seen recently."""
    seen: dict[str, float] = state.setdefault("seen", {})
    for token, ts in list(seen.items()):
        if now - ts > SEEN_TTL_SECONDS:
            del seen[token]

    per_group: dict[str, int] = {}
    opened: list[dict] = []
    for row in rows:
        token = row["token"]
        if token in seen:
            continue
        groups = classify(row, now)
        if not groups:
            continue
        kept = [g for g in groups if per_group.get(g, 0) < MAX_NEW_PER_GROUP]
        if not kept:
            continue
        for group in kept:
            per_group[group] = per_group.get(group, 0) + 1
        seen[token] = now
        opened.append(
            {
                "token": token,
                "symbol": row["symbol"],
                "pool": row["pool"],
                "groups": kept,
                "entry_ts": int(now),
                "entry_price_api": row["price_usd"],
                "reserve_usd": row["reserve_usd"],
            }
        )
    state.setdefault("open", []).extend(opened)
    return opened


def split_matured(state: dict, now: float) -> list[dict]:
    matured = [p for p in state.get("open", []) if now - p["entry_ts"] >= MATURITY_SECONDS]
    state["open"] = [p for p in state.get("open", []) if now - p["entry_ts"] < MATURITY_SECONDS]
    return matured


def aggregate(ledger_rows: list[dict]) -> dict:
    """group -> strategy -> {n, wins, sum_net}."""
    totals: dict = {}
    for row in ledger_rows:
        cell = totals.setdefault(row["group"], {}).setdefault(
            row["strategy"], {"n": 0, "wins": 0, "sum_net": 0.0}
        )
        net = float(row["net_return_pct"])
        cell["n"] += 1
        cell["wins"] += 1 if net > 0 else 0
        cell["sum_net"] += net
    return totals


async def run() -> None:
    state_dir = Path(os.environ.get("STATE_DIR") or "state")
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "paper_state.json"
    ledger_path = state_dir / "ledger.csv"

    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    now = time.time()

    from .gecko import Gecko

    gecko = Gecko()
    try:
        rows = await gecko.pools("trending_pools", 3)
        fresh = await gecko.pools("new_pools", 5)
        known = {r["token"] for r in rows}
        rows += [r for r in fresh if r["token"] not in known]

        opened = open_positions(rows, state, now)
        matured = split_matured(state, now)

        scored_rows: list[dict] = []
        unscoreable = 0

        def record(position: dict, strategy: str, outcome: dict) -> None:
            for group in position["groups"]:
                scored_rows.append(
                    {
                        "scored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "token": position["token"],
                        "symbol": position["symbol"],
                        "group": group,
                        "strategy": strategy,
                        "entry_ts": position["entry_ts"],
                        "exit_reason": outcome["exit_reason"],
                        "net_return_pct": outcome["net_return_pct"],
                    }
                )

        for position in matured:
            candles = Candles(
                rows=await gecko.candles(
                    position["pool"], position["entry_ts"] + MATURITY_SECONDS
                )
            )
            any_scored = False
            for strategy, (tp, sl, hold) in EXIT_GRID.items():
                outcome = simulate_exit(
                    candles, position["entry_ts"],
                    take_profit_pct=tp, stop_loss_pct=sl,
                    hold_hours=hold, round_trip_fee_pct=FEES_PCT,
                )
                if outcome is None:
                    continue
                any_scored = True
                record(position, strategy, outcome)
            if not any_scored:
                unscoreable += 1
            else:
                # Runner metrics need the longer window; ride it and rescore.
                state.setdefault("riding", []).append(position)

        riding = [p for p in state.get("riding", []) if now - p["entry_ts"] >= RIDE_MATURITY_SECONDS]
        state["riding"] = [p for p in state.get("riding", []) if now - p["entry_ts"] < RIDE_MATURITY_SECONDS]
        for position in riding:
            candles = Candles(
                rows=await gecko.candles(
                    position["pool"], position["entry_ts"] + RIDE_MATURITY_SECONDS
                )
            )
            moonbag = simulate_moonbag(
                candles, position["entry_ts"],
                trigger_gain_pct=100, sell_fraction=0.5,
                window_hours=RIDE_HOURS, round_trip_fee_pct=FEES_PCT,
            )
            if moonbag:
                record(position, "moonbag_2x_half_72h", moonbag)
            peak = max_gain_pct(candles, position["entry_ts"], RIDE_HOURS)
            if peak is not None:
                # Encoded so the table's win% column reads as the 2x hit rate.
                record(position, "hit_2x_within_72h",
                       {"exit_reason": f"peak_{peak}", "net_return_pct": 100.0 if peak >= 100 else -0.01})
                record(position, "max_gain_72h",
                       {"exit_reason": "peak", "net_return_pct": peak})

        header = ["scored_at", "token", "symbol", "group", "strategy",
                  "entry_ts", "exit_reason", "net_return_pct"]
        write_header = not ledger_path.exists()
        with ledger_path.open("a", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=header)
            if write_header:
                writer.writeheader()
            writer.writerows(scored_rows)

        state_path.write_text(json.dumps(state, indent=1))

        all_rows = list(csv.DictReader(ledger_path.open(encoding="utf-8"))) if ledger_path.exists() else []
        totals = aggregate(all_rows)

        lines = [
            f"# Paper trader tick {datetime.now(timezone.utc).isoformat(timespec='minutes')}",
            f"Opened {len(opened)} new positions; scored {len(matured) - unscoreable} "
            f"at 26h ({unscoreable} had no price data); rescored {len(riding)} at 75h; "
            f"{len(state.get('open', []))} open, {len(state.get('riding', []))} riding.",
            "",
            "| Group | Strategy | Trades | Win % | Avg net % |",
            "|---|---|---|---|---|",
        ]
        strategy_order = list(EXIT_GRID) + ["moonbag_2x_half_72h", "hit_2x_within_72h", "max_gain_72h"]
        for group in sorted(totals):
            for strategy in strategy_order:
                cell = totals[group].get(strategy)
                if not cell:
                    continue
                lines.append(
                    f"| {group} | {strategy} | {cell['n']} | "
                    f"{100 * cell['wins'] / cell['n']:.0f} | "
                    f"{cell['sum_net'] / cell['n']:.2f} |"
                )
        if not any(totals.values()):
            lines.append("| (no scored trades yet) | | | | |")
        report = "\n".join(lines)
        print(report, flush=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(report + "\n")
    finally:
        await gecko.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
