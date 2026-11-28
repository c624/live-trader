# Slow Maker: paper market making on weather brackets

Started 2026-10-25T23:23:53+00:00; tick 9519, 94 markets quoted now. Quotes 1c inside the best bid and ask, 10 a side, inventory capped at 50 per market, refreshed about every 5 minutes; maker fee charged on every fill.

**Verdict:** **Failed**: the 95% interval does not lie above zero.

| Fills | Contracts | Spread earned | Fees | Settled city-days | P&L per contract (settled) | 95% interval | Open inventory |
|---|---|---|---|---|---|---|---|
| 128494 | 514988 | $+8,358.97 | $2,320.38 | 771 | -2.07c | -2.28c to -1.86c | 2543 |

Markouts (mid after the fill against our price, per contract; negative means picked off): 5 min -0.96c over 514857, 60 min -1.32c over 513138.

Settled P&L so far: $-10,479.62.

