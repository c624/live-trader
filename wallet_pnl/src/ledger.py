"""Realized profit and loss for a wallet, from its own swaps.

Every previous copy-trade test simulated OUR exit rules on THEIR entries and
asked whether that made money. It never asked the prior question: did the
wallet itself make money? A trader can be excellent while a +20%/-30% rule
bolted onto their entries loses, and a spray bot can look busy while
bleeding. This module answers the prior question by accounting rather than
simulation, so none of the biases that wrecked the backtests apply: there is
no exit model, no candle data, and nothing can drop out of the sample.

Positions use average cost. A token that was bought and never sold leaves an
open bag, and because a memecoin bag is usually worthless the report carries
both bounds: realized profit alone (generous, ignores the bags) and realized
profit with every open bag written to zero (harsh). A wallet worth copying
should look good under both.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Swap:
    """One leg of trading against SOL.

    token_amount is positive when the wallet acquired the token, negative
    when it disposed of it. sol_amount has the opposite sign: negative when
    SOL was spent, positive when SOL came back.
    """

    ts: int
    signature: str
    mint: str
    token_amount: float
    sol_amount: float


@dataclass
class TokenResult:
    mint: str
    sol_spent: float = 0.0
    sol_received: float = 0.0
    tokens_bought: float = 0.0
    tokens_sold: float = 0.0
    first_ts: int = 0
    last_ts: int = 0
    buys: int = 0
    sells: int = 0

    @property
    def cost_per_token(self) -> float:
        return self.sol_spent / self.tokens_bought if self.tokens_bought else 0.0

    @property
    def tokens_held(self) -> float:
        # Selling more than was bought means tokens arrived from somewhere we
        # cannot see; treat the position as flat rather than negative.
        return max(0.0, self.tokens_bought - self.tokens_sold)

    @property
    def open_cost_sol(self) -> float:
        return self.tokens_held * self.cost_per_token

    @property
    def realized_sol(self) -> float:
        """Proceeds minus the cost of the tokens actually sold."""
        sold = min(self.tokens_sold, self.tokens_bought)
        return self.sol_received - sold * self.cost_per_token

    @property
    def fully_exited(self) -> bool:
        return self.tokens_bought > 0 and self.tokens_held <= self.tokens_bought * 0.01


@dataclass
class WalletReport:
    wallet: str
    tokens: dict[str, TokenResult] = field(default_factory=dict)
    swaps: int = 0
    first_ts: int = 0
    last_ts: int = 0

    @property
    def sol_deployed(self) -> float:
        return sum(t.sol_spent for t in self.tokens.values())

    @property
    def realized_sol(self) -> float:
        return sum(t.realized_sol for t in self.tokens.values())

    @property
    def open_cost_sol(self) -> float:
        return sum(t.open_cost_sol for t in self.tokens.values())

    @property
    def realized_pct(self) -> float:
        """Return on SOL deployed, counting only positions that were closed."""
        return 100 * self.realized_sol / self.sol_deployed if self.sol_deployed else 0.0

    @property
    def harsh_pct(self) -> float:
        """The same, with every unsold bag written down to zero."""
        if not self.sol_deployed:
            return 0.0
        return 100 * (self.realized_sol - self.open_cost_sol) / self.sol_deployed

    @property
    def closed(self) -> list[TokenResult]:
        return [t for t in self.tokens.values() if t.fully_exited]

    @property
    def win_rate_pct(self) -> float:
        closed = self.closed
        if not closed:
            return 0.0
        return 100 * sum(1 for t in closed if t.realized_sol > 0) / len(closed)

    @property
    def days_active(self) -> float:
        return max((self.last_ts - self.first_ts) / 86400, 1 / 24)

    @property
    def buys_per_day(self) -> float:
        return sum(t.buys for t in self.tokens.values()) / self.days_active

    @property
    def median_buy_sol(self) -> float:
        sizes = sorted(
            t.sol_spent / t.buys for t in self.tokens.values() if t.buys
        )
        if not sizes:
            return 0.0
        mid = len(sizes) // 2
        return sizes[mid] if len(sizes) % 2 else (sizes[mid - 1] + sizes[mid]) / 2


def build_report(wallet: str, swaps: list[Swap]) -> WalletReport:
    """Fold a wallet's swaps into per-token positions. Order does not matter."""
    report = WalletReport(wallet=wallet)
    ordered = sorted(swaps, key=lambda s: s.ts)
    for swap in ordered:
        if not swap.mint or swap.token_amount == 0:
            continue
        token = report.tokens.setdefault(swap.mint, TokenResult(mint=swap.mint))
        if not token.first_ts:
            token.first_ts = swap.ts
        token.last_ts = swap.ts
        if swap.token_amount > 0:
            token.tokens_bought += swap.token_amount
            token.sol_spent += max(0.0, -swap.sol_amount)
            token.buys += 1
        else:
            token.tokens_sold += -swap.token_amount
            token.sol_received += max(0.0, swap.sol_amount)
            token.sells += 1
        report.swaps += 1
    if ordered:
        report.first_ts, report.last_ts = ordered[0].ts, ordered[-1].ts
    return report


def split_by_time(swaps: list[Swap], boundary_ts: int) -> tuple[list[Swap], list[Swap]]:
    """Earlier and later halves for the persistence test.

    A token bought before the boundary and sold after it would land its cost
    in one period and its proceeds in the other, which would invent profits
    and losses that nobody made. Such tokens are dropped from both halves.
    """
    sides: dict[str, set[bool]] = {}
    for swap in swaps:
        sides.setdefault(swap.mint, set()).add(swap.ts < boundary_ts)
    straddling = {mint for mint, seen in sides.items() if len(seen) > 1}
    early = [s for s in swaps if s.ts < boundary_ts and s.mint not in straddling]
    late = [s for s in swaps if s.ts >= boundary_ts and s.mint not in straddling]
    return early, late
