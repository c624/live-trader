"""A hard ceiling on API calls, because nothing was counting them.

A single websocket measurement pushed 950,616 messages through the Helius
plan in eight minutes and exhausted a month of credits. Nothing in the code
was watching, so the first anyone knew was an HTTP 429 during a pre-flight,
twenty-two days from a reset, with a wallet that cannot be traded because the
same account signs and sells.

The lesson is not "use less". It is that consumption has to be counted and
capped by something that stops rather than warns, and that discovery must
never be able to spend the budget the exits depend on.
"""

from __future__ import annotations

import threading


class Budget:
    """Counts calls against a ceiling and refuses once it is reached.

    Reserved calls are held back for trading. Discovery draws from the
    general pool and stops when it is empty; selling draws from the reserve
    and keeps working, because a position that cannot be sold is worse than
    a launch that is never seen.
    """

    def __init__(self, ceiling: int, reserved_for_trading: int = 200):
        self._ceiling = ceiling
        self._reserved = min(reserved_for_trading, ceiling)
        self._lock = threading.Lock()
        self.spent = 0
        self.refused = 0

    @property
    def discovery_ceiling(self) -> int:
        return max(0, self._ceiling - self._reserved)

    @property
    def remaining(self) -> int:
        with self._lock:
            return max(0, self._ceiling - self.spent)

    def take(self, purpose: str = "discovery", cost: int = 1) -> bool:
        """Claim budget for one call. False means do not make it."""
        with self._lock:
            limit = self._ceiling if purpose == "trading" else self.discovery_ceiling
            if self.spent + cost > limit:
                self.refused += 1
                return False
            self.spent += cost
            return True

    def summary(self) -> str:
        with self._lock:
            return (f"budget: {self.spent}/{self._ceiling} used "
                    f"({self.discovery_ceiling} of it available to discovery), "
                    f"{self.refused} calls refused")
