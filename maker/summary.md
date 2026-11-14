# Slow Maker: paper market making on weather brackets

Started 2026-10-25T23:23:53+00:00; tick 5549, 68 markets quoted now. Quotes 1c inside the best bid and ask, 10 a side, inventory capped at 50 per market, refreshed about every 5 minutes; maker fee charged on every fill.

**Verdict:** **Failed**: the 95% interval does not lie above zero.

| Fills | Contracts | Spread earned | Fees | Settled city-days | P&L per contract (settled) | 95% interval | Open inventory |
|---|---|---|---|---|---|---|---|
| 72597 | 293053 | $+4,460.93 | $1,312.71 | 437 | -2.11c | -2.38c to -1.84c | 2569 |

Markouts (mid after the fill against our price, per contract; negative means picked off): 5 min -0.93c over 292875, 60 min -1.28c over 291632.

Settled P&L so far: $-5,983.79.

