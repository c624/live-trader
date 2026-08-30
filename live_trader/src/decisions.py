"""Pure decision logic, no IO: which pools to buy, when to exit.

Keeping these as plain functions of plain data is what makes the safety
rails testable offline; the loop in run_live is only plumbing around them.
"""

from __future__ import annotations

from datetime import datetime, timezone


def parse_created(created: str | None) -> float | None:
    if not created:
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def utc_day(now: float) -> str:
    return datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")


def entry_filter(row: dict, now: float, cfg: dict) -> str | None:
    """None if the row qualifies, else the reason it does not.

    Mirrors the paper trader's brand_new group: the strategy being piloted
    is exactly the one the lab measured, no silent widening.
    """
    entry = cfg["entry"]
    reserve = row.get("reserve_usd")
    if reserve is None or row.get("price_usd") is None or not row.get("pool"):
        return "missing_data"
    if reserve < entry["min_reserve_usd"]:
        return "reserve_too_low"
    created = parse_created(row.get("pool_created_at"))
    if created is None:
        return "no_creation_time"
    age_hours = (now - created) / 3600
    if age_hours < 0:
        return "clock_skew"
    if age_hours > entry["max_age_hours"]:
        return "too_old"
    # The lab's edge was measured on pools up to an hour old at the 2h tick;
    # a minutes-old pool is a different (rug-heavier) population. Too-young
    # pools are NOT marked seen, so they re-qualify once they age in.
    if age_hours < entry.get("min_age_hours", 0.0):
        return "too_young"
    return None


def pick_entries(rows: list[dict], state: dict, now: float, cfg: dict) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Returns (buys, skipped) where skipped is [(row, reason)].

    Caps are enforced in order: dedupe, position limit, daily spend.
    """
    entry = cfg["entry"]
    seen = state.setdefault("seen", {})
    ttl = entry["seen_ttl_days"] * 86400
    for mint, ts in list(seen.items()):
        if now - ts > ttl:
            del seen[mint]

    held = {p["mint"] for p in state["positions"] if p.get("status") != "closed"}
    open_count = len(held)
    spent_today = state.setdefault("daily_spend", {}).get(utc_day(now), 0.0)

    buys: list[dict] = []
    skipped: list[tuple[dict, str]] = []
    for row in rows:
        mint = row["token"]
        reason = entry_filter(row, now, cfg)
        if reason is None and mint in seen:
            reason = "already_seen"
        if reason is None and mint in held:
            reason = "already_held"
        if reason is None:
            seen[mint] = now
            if len(buys) >= entry["max_new_per_loop"]:
                reason = "loop_cap"
            elif open_count + len(buys) >= cfg["max_open_positions"]:
                reason = "position_cap"
            elif spent_today + (len(buys) + 1) * cfg["ticket_usd"] > cfg["max_daily_spend_usd"]:
                reason = "daily_spend_cap"
        if reason is None:
            buys.append(row)
        else:
            skipped.append((row, reason))
    return buys, skipped


def exit_reason(position: dict, value_usd: float | None, now: float, cfg: dict) -> str | None:
    """'tp' | 'sl' | 'time' | 'dead' | None.

    value_usd is what the whole position sells for right now per Jupiter's
    executable quote; None means no route exists (usually a pulled pool).
    """
    ex = cfg["exit"]
    age_hours = (now - position["opened_ts"]) / 3600
    if value_usd is None:
        # Sustained absence of any sell route is a rug, not a blip. Waiting
        # the full hold window to admit it only delays the write-off and
        # holds a slot hostage; ~40 failed checks is an hour of silence.
        if position.get("no_route_checks", 0) >= 40:
            return "dead"
        if age_hours > ex["hold_hours"] and position.get("no_route_checks", 0) >= 2:
            return "dead"
        return None
    cost = position["cost_usd"]
    if cost <= 0:
        return "dead"
    pnl_pct = (value_usd / cost - 1.0) * 100.0
    if pnl_pct >= ex["tp_pct"]:
        return "tp"
    if pnl_pct <= -ex["sl_pct"]:
        return "sl"
    if age_hours >= ex["hold_hours"]:
        return "time"
    return None
