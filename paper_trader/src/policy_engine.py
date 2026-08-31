"""Exit policies scored on the same honest terms as the corrected engine.

The rug-filter study showed the losses are not concentrated in pools we
could have screened out: even a population with a 5.9% rug rate lost 24.8%
per trade, because 60.8% of those trades hit the stop and filled at -59%.
That points at the exit rules rather than the entry, so this module scores
arbitrary take-profit / stop / trailing-stop / hold combinations.

Every honest rule from the corrected engine carries over, plus one more it
was missing: a pool that stops trading before the hold expires cannot be
sold at its last printed price. The old code quietly used that price at
timeout, which flattered long holds. Here a stale tail is a rug.
"""

from __future__ import annotations

from dataclasses import dataclass

from .honest_engine import SILENCE_IS_DEATH_SECONDS, Outcome


@dataclass(frozen=True)
class Policy:
    """None disables a leg: no take-profit, no fixed stop, no trailing stop."""

    take_profit_pct: float | None
    stop_loss_pct: float | None
    trail_pct: float | None
    hold_hours: float
    round_trip_fee_pct: float = 3.2

    @property
    def label(self) -> str:
        def part(name, value):
            return f"{name}{value:g}" if value is not None else f"{name}-"
        return (
            f"{part('tp', self.take_profit_pct)} "
            f"{part('sl', self.stop_loss_pct)} "
            f"{part('tr', self.trail_pct)} "
            f"h{self.hold_hours:g}"
        )


def simulate_policy(candles, entry_ts: int, policy: Policy) -> Outcome:
    """Score one position under one exit policy. Always returns an outcome."""
    entry_price = candles.price_at(entry_ts)
    if not entry_price:
        return Outcome("no_entry_price", 0.0, False)

    hold_seconds = int(policy.hold_hours * 3600)
    # A two-hour silence rule says nothing about a five-minute trade. What
    # matters is whether the pool was trading near the moment we wanted out,
    # so the threshold is a quarter of the hold - never less than two minutes,
    # never more than the two hours the long holds already used. Tying it to
    # the full hold would let a thirty-minute trade tolerate twenty-nine
    # minutes of silence and still call the exit a real fill.
    silence = min(SILENCE_IS_DEATH_SECONDS, max(120, hold_seconds // 4))
    window_end = entry_ts + hold_seconds
    # The entry price is the CLOSE of the bar covering entry_ts, so the buy
    # happens at the end of that bar. Scanning that same bar would credit its
    # high and low to a position taken after they printed - a spike inside the
    # first minute would book a take-profit nobody could have taken. Only bars
    # that open after the fill are tradeable.
    entry_bar_ts = max(
        (r[0] for r in candles.rows if r[0] <= entry_ts), default=entry_ts
    )
    rows = [r for r in candles.rows if entry_bar_ts < r[0] <= window_end]
    if not rows:
        return Outcome("rug_no_data", -100.0, False)

    last_stamp = entry_bar_ts
    for row in rows:
        if row[0] - last_stamp > silence:
            return Outcome("rug_went_quiet", -100.0, False)
        last_stamp = row[0]

    fee = policy.round_trip_fee_pct
    take_profit = (
        entry_price * (1 + policy.take_profit_pct / 100)
        if policy.take_profit_pct is not None
        else None
    )
    hard_stop = (
        entry_price * (1 - policy.stop_loss_pct / 100)
        if policy.stop_loss_pct is not None
        else None
    )

    peak = entry_price
    for _stamp, _open, high, low, _close in rows:
        # The trailing level uses the peak from BEFORE this bar: a bar cannot
        # set a new high and then be stopped out against it.
        levels = [lvl for lvl in (hard_stop,) if lvl is not None]
        if policy.trail_pct is not None:
            levels.append(peak * (1 - policy.trail_pct / 100))
        exit_level = max(levels) if levels else None

        if exit_level is not None and low <= exit_level:
            # Fill at the low: the bar is the only evidence of how far price
            # ran before anyone could get out.
            gross = (low / entry_price - 1) * 100
            reason = "trail_stop" if exit_level != hard_stop else "stop_loss"
            return Outcome(reason, round(gross - fee, 2), True)

        if take_profit is not None and high >= take_profit:
            return Outcome(
                "take_profit", round(policy.take_profit_pct - fee, 2), False
            )

        peak = max(peak, high)

    # Selling at timeout means selling into whatever is still trading. If the
    # pool went silent before the window closed, there is nothing to sell to.
    if window_end - rows[-1][0] > silence:
        return Outcome("rug_went_quiet", -100.0, False)

    last = candles.price_at(window_end) or rows[-1][4]
    gross = (last / entry_price - 1) * 100
    return Outcome("held_to_timeout", round(gross - fee, 2), False)
