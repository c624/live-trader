"""Exit simulation, identical rules to the wallet grader's engine.

Ambiguous bars (touching take-profit and stop in the same bar) resolve to
the stop. A paper trader that flatters itself is worse than none.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Candles:
    """Bars oldest-first as (unix_seconds, open, high, low, close)."""

    rows: list[tuple[int, float, float, float, float]] = field(default_factory=list)

    def price_at(self, when: int) -> float | None:
        chosen = None
        for row in self.rows:
            if row[0] <= when:
                chosen = row[4]
            else:
                break
        return chosen


def simulate_exit(
    candles: Candles,
    entry_ts: int,
    *,
    take_profit_pct: float,
    stop_loss_pct: float,
    hold_hours: int,
    round_trip_fee_pct: float,
) -> dict | None:
    entry_price = candles.price_at(entry_ts)
    if not entry_price:
        return None

    take_profit = entry_price * (1 + take_profit_pct / 100)
    stop_loss = entry_price * (1 - stop_loss_pct / 100)
    window_end = entry_ts + hold_hours * 3600

    exit_reason = "held_to_timeout"
    gross = None
    for stamp, _open, high, low, close in candles.rows:
        if stamp < entry_ts:
            continue
        if stamp > window_end:
            break
        if low <= stop_loss:
            exit_reason, gross = "stop_loss", -stop_loss_pct
            break
        if high >= take_profit:
            exit_reason, gross = "take_profit", take_profit_pct
            break

    if gross is None:
        last = candles.price_at(window_end) or entry_price
        gross = (last / entry_price - 1) * 100

    return {
        "exit_reason": exit_reason,
        "net_return_pct": round(gross - round_trip_fee_pct, 2),
    }


# Name -> (take profit %, stop %). 9999 means never take profit; stop 99 with
# tp 9999 is a pure buy-and-hold marked at the window end.
# name -> (take profit %, stop %, hold hours). 9999 means never take profit.
# The 72h runner metrics showed every group losing money by hour 72 despite
# a quarter of coins doubling somewhere along the way: the fat tail is real
# but fast. These shorter windows test how far "get out sooner" goes. They
# cost nothing extra to score, since the candles are already fetched.
EXIT_GRID = {
    "tp20_sl30": (20, 30, 24),
    "tp50_sl30": (50, 30, 24),
    "tp100_sl50": (100, 50, 24),
    "noTP_sl50": (9999, 50, 24),
    "hold24h": (9999, 99, 24),
    "tp100_sl50_3h": (100, 50, 3),
    "tp100_sl50_6h": (100, 50, 6),
    "tp100_sl50_12h": (100, 50, 12),
    "tp50_sl30_6h": (50, 30, 6),
}


def max_gain_pct(candles: Candles, entry_ts: int, window_hours: int) -> float | None:
    """Highest point reached within the window, relative to entry."""
    entry_price = candles.price_at(entry_ts)
    if not entry_price:
        return None
    end = entry_ts + window_hours * 3600
    highs = [r[2] for r in candles.rows if entry_ts <= r[0] <= end]
    if not highs:
        return None
    return round((max(highs) / entry_price - 1) * 100, 2)


def simulate_moonbag(
    candles: Candles,
    entry_ts: int,
    *,
    trigger_gain_pct: float,
    sell_fraction: float,
    window_hours: int,
    round_trip_fee_pct: float,
) -> dict | None:
    """Sell a fraction at the trigger, ride the rest to the window end.

    This is the exit structure a runner-hunting thesis actually needs: the
    recouped half caps the damage, the riding half keeps the right tail.
    """
    entry_price = candles.price_at(entry_ts)
    if not entry_price:
        return None
    end = entry_ts + window_hours * 3600
    trigger_price = entry_price * (1 + trigger_gain_pct / 100)

    triggered = False
    for stamp, _open, high, _low, _close in candles.rows:
        if stamp < entry_ts:
            continue
        if stamp > end:
            break
        if high >= trigger_price:
            triggered = True
            break

    final_price = candles.price_at(end) or entry_price
    tail_return = (final_price / entry_price - 1) * 100
    if triggered:
        gross = sell_fraction * trigger_gain_pct + (1 - sell_fraction) * tail_return
        reason = "moonbag_triggered"
    else:
        gross = tail_return
        reason = "never_triggered"
    return {
        "exit_reason": reason,
        "net_return_pct": round(gross - round_trip_fee_pct, 2),
    }
