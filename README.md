# live_trader

Automated Solana memecoin pilot. Detects brand-new pools with the same
filter the paper-trading lab validated, buys a fixed small ticket through
Jupiter, and exits on take-profit, stop-loss, or a hold timer. Runs as a
GitHub Actions loop; state (positions, trade log, signal log) lives on the
`live-state` branch.

## Safety rails

- Ships with `"trading_enabled": false` in `config.json`. Until that is
  flipped AND the wallet secret exists, it is a signal logger only.
- `DRY_RUN=1` in the environment force-disables trading regardless of config.
- Kill switch: create a file named `KILL` in the repo root (GitHub app ->
  Add file) to stop new buys; `KILL_ALL` also stops sells. Delete to resume.
- Hard caps in `config.json`: ticket size, max open positions, max daily
  spend, max price impact per trade.
- The wallet must only ever hold pilot money. Total loss must be survivable.

## Secrets (Repo Settings -> Secrets and variables -> Actions)

| Secret | What it is |
| --- | --- |
| `TRADER_PRIVATE_KEY` | Base58 private key of the dedicated trading wallet (Phantom: Settings -> Manage Accounts -> account -> Show Private Key). Never paste it anywhere else, ever. |
| `HELIUS_API_KEY` | A fresh Helius API key (rotate any key that has ever been pasted in a chat). |
| `TELEGRAM_BOT_TOKEN` | Token from @BotFather (optional; alerts are skipped without it). |
| `TELEGRAM_CHAT_ID` | Your chat id; run `python -m live_trader.src.notify` after messaging the bot to discover it. |
| `X_BEARER_TOKEN` | Optional. Bearer token from an X developer account on pay-per-use billing, with a hard spending cap set on the X side first. Without it the social arms simply never fire. The loop keeps its own read budget (`social_reads` in the paper ledger, 19,000 reads) and stops searching when it is spent. |

### The social test

Three paper arms compare tokens whose contract address is being posted on X
against tokens from the same population that were searched and not mentioned:
`social` (two or more authors in the last hour, 10-minute hold), `social1h`
(same entry, 1-hour hold) and `socctl` (searched, zero mentions). The bar was
fixed before the first read: `social` must beat `socctl` with non-overlapping
95% intervals over at least 100 closed trades, and mention velocity must
separate outcomes in the feature scorer. Anything short of that ends the test.

## How a trade happens

1. Every ~4 minutes the loop pulls GeckoTerminal's newest Solana pools.
2. A pool qualifies if it is under `max_age_hours` old with at least
   `min_reserve_usd` liquidity (the lab's `brand_new` group, unchanged).
3. Buy: fixed `ticket_usd` ticket via Jupiter quote -> swap -> Helius send,
   skipped if price impact exceeds `max_price_impact_pct`.
4. Every ~75 seconds each open position is re-quoted at executable size.
   Take-profit, stop-loss, and the hold timer close it; a position with no
   sell route after the hold window is written off as a rug.
5. Every action appends to `trades.csv` / `signals.csv` on `live-state`,
   which is what the paper-vs-live comparison reads.
