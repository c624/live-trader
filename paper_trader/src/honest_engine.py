"""Exit simulation that does not flatter the strategy.

The original engine made one assumption that live trading disproved: that a
stop-loss always fills at exactly the stop price. It does not. A memecoin
that craters through the stop inside a single candle fills far below it, and
a pool whose liquidity is pulled cannot be sold at all. The original also
dropped positions whose pools stopped producing candles, which is precisely
what a rugged pool does, so the worst outcomes left the sample entirely.

This module scores the same positions with three corrections:

  1. a stop fills at the candle's low, not at the stop price
  2. a pool that goes quiet after entry is a rug, scored -100%, not dropped
  3. every position gets an outcome, and the reasons are counted

Nothing here is tuned to produce a nicer number; each change can only move
results down, which is the point.
"""

from __future__ import annotations

from dataclasses import dataclass

# A pool that produces no candle inside this window after entry is treated as
# dead rather than merely illiquid. Two hours is far longer than any gap seen
# in healthy pools that trade at all.
SILENCE_IS_DEATH_SECONDS = 2 * 3600


@dataclass
class Outcome:
    exit_reason: str
    net_return_pct: float
    modelled_fill: bool  # True when the fill price came from a candle low


def _rows_after(candles, entry_ts: int, window_end: int) -> list[tuple]:
    return [r for r in candles.rows if entry_ts <= r[0] <= window_end]


def simulate_exit_honest(
    candles,
    entry_ts: int,
    *,
    take_profit_pct: float,
    stop_loss_pct: float,
    hold_hours: int,
    round_trip_fee_pct: float,
) -> Outcome:
    """Score one position. Always returns an outcome, never None."""
    entry_price = candles.price_at(entry_ts)
    if not entry_price:
        # No price at entry at all: the position could not have been taken in
        # the first place, so this is a data gap rather than a trade.
        return Outcome("no_entry_price", 0.0, False)

    window_end = entry_ts + hold_hours * 3600
    rows = _rows_after(candles, entry_ts, window_end)
    if not rows:
        # Bought, then the pool never printed another candle: a rug.
        return Outcome("rug_no_data", -100.0, False)

    # A long silence inside the window is the same event with a later start.
    last_stamp = entry_ts
    for stamp, _open, _high, _low, _close in rows:
        if stamp - last_stamp > SILENCE_IS_DEATH_SECONDS:
            return Outcome("rug_went_quiet", -100.0, False)
        last_stamp = stamp

    take_profit = entry_price * (1 + take_profit_pct / 100)
    stop_loss = entry_price * (1 - stop_loss_pct / 100)

    for stamp, _open, high, low, close in rows:
        if low <= stop_loss:
            # Fill at the low, not at the stop: the candle is the only
            # evidence of how far price ran before anyone could sell.
            gross = (low / entry_price - 1) * 100
            return Outcome("stop_loss", round(gross - round_trip_fee_pct, 2), True)
        if high >= take_profit:
            return Outcome(
                "take_profit", round(take_profit_pct - round_trip_fee_pct, 2), False
            )

    last = candles.price_at(window_end) or rows[-1][4]
    gross = (last / entry_price - 1) * 100
    return Outcome("held_to_timeout", round(gross - round_trip_fee_pct, 2), False)


def summarize(outcomes: list[Outcome]) -> dict:
    """Per-reason counts and means plus the weighted edge they imply."""
    by_reason: dict[str, list[float]] = {}
    for outcome in outcomes:
        by_reason.setdefault(outcome.exit_reason, []).append(outcome.net_return_pct)
    total = len(outcomes)
    rows = []
    edge = 0.0
    for reason, values in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        share = len(values) / total
        mean = sum(values) / len(values)
        edge += share * mean
        rows.append(
            {
                "reason": reason,
                "n": len(values),
                "share_pct": round(100 * share, 1),
                "mean_pct": round(mean, 2),
                "contribution_pct": round(share * mean, 2),
            }
        )
    return {"n": total, "rows": rows, "edge_pct": round(edge, 2)}
