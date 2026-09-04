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

### The social test (closed 2026-09-02)

Tokens whose address was being posted on X, against tokens from the same
feed that were searched and not mentioned. The one-author arm ended at 85
trades on the fee floor with its interval overlapping the control's; the
two-author arms were the worst in the study. The X feed is only read while
an arm filters on a mention field, so with none configured it costs nothing.

### The graduation-signal test (closed 2026-09-03)

Public data on 830,000 launches says two things visible at creation predict
a token leaving the bonding curve: a Telegram link in its metadata (about
nine times the graduation rate) and the creator's own buy. Graduation is not
a trade, so each is a paper arm beside a control from its own population:

- `links` (Telegram present) and `nolinks` (metadata read, none present),
  both 10-minute holds on the launch feed; unknown metadata is in neither.
- `devbuy` (creator put in 1 SOL or more) against `launchctl`, the plain
  launch-feed population with the same hold.
- `surgeliq` and `surgeliq1h` (a five-minute buy surge in a graduated pool
  holding $10k-50k of liquidity) against `liqctl`, that liquidity band with
  no signal. A post-hoc slice of the earlier `surge` arm suggested this band;
  the arm exists to test it on data that did not.

Addendum, 23:15 UTC the same day: the first 74 metadata reads found a
Twitter link on 50 and a website on 29, and a Telegram on none. The launch
form appears not to carry one any more, so `links` may never fire. Rather
than change it, `twitter` (Twitter link present) and `notwitter` (metadata
read, none present) were added beside it under the same bar.

The bar, fixed before the first trade: a signal arm passes only with 100 or
more closed trades AND a 95% interval lying entirely above its control's.
Anything short of that closes the arm.

Result, 23:11 UTC on 2026-09-03, after 26 hours:

| arm | rule | n | mean | median | 95% interval | vs control |
|---|---|---|---|---|---|---|
| devbuy | creator buy >= 1 SOL | 685 | -4.7% | -2.6% | -6.6% to -2.9% | fail (launchctl -7.5%, intervals overlap) |
| twitter | Twitter link present | 696 | -3.4% | -2.7% | -5.7% to -1.1% | fail (notwitter -5.9%, intervals overlap) |
| links | Telegram present | 42 | -11.0% | -2.8% | -22.5% to +0.5% | fail (never reached 100; nolinks -3.5%) |
| surgeliq | surge in a $10k-50k pool, 10-min hold | 106 | +4.2% | +3.9% | -10.9% to +19.3% | fail (liqctl +2.0%, -8.3% to +12.4%) |
| surgeliq1h | same, one-hour hold | 103 | +30.7% | -11.9% | -3.6% to +64.9% | fail (lower bound below liqctl's upper) |

Every launch-side signal lost, by less than its control but still below
zero: a creator buy and a Twitter link shave two or three points off a
losing population and nothing more. The ten-minute surge arm is its
control with a different name (mean without its three best trades -2.1%,
the control's -2.1%).

The one-hour surge arm is the only thing in 13,700 paper trades whose
number is large and positive: +$63 on $206 staked, 43 of 103 trades
reaching the +100% take-profit and 48 going to zero or near it. It fails
the bar because its interval still spans zero, and the bar exists so that
a single +1232% trade does not decide the question. It also carries a
flaw in the test's own design: it held for an hour against a control that
held for ten minutes, so the comparison cannot say whether the surge or
the hold did the work.

### The hold-time test (stopped 2026-09-04)

The flaw above is fixed by one more control: `liqctl1h`, the same $10k-50k
graduated-pool population with no signal, held for one hour. `surgeliq1h`
keeps running unchanged beside it. Pre-registered before the control's
first trade:

- Only trades closed at or after 2026-09-04 00:00 UTC count for either arm
  (`python -m live_trader.src.paper_score state/paper --since=2026-09-04T00:00:00Z`).
- `surgeliq1h` passes only with 100 or more closed trades in that window
  AND a 95% interval lying entirely above `liqctl1h`'s.
- Anything short of that closes the graduated-pool line of work.

Stopped at 18:23 UTC on 2026-09-04 on Carter's instruction, two trades
short of the count. The window at the stop:

| arm | n | mean | median | win | 95% interval |
|---|---|---|---|---|---|
| surgeliq1h | 98 | -6.1% | -99.9% | 32% | -38.5% to +26.4% |
| liqctl1h | 231 | -15.9% | -97.4% | 28% | -33.8% to +1.9% |
| liqctl | 241 | -9.2% | -6.1% | 37% | -22.8% to +4.4% |

The surge arm's interval never left zero and never separated from its
control; the bar could not have been met by the two missing trades. With
a proper one-hour control beside it, the +30% reading of the day before
resolved into a losing arm whose mean is held up by a few take-profits
against a majority of total losses. That closes the graduated-pool line
and, with it, this project: over 17,240 paper trades and every entry
signal available at trade time, nothing beat its own control.

The loop's schedule and self-chain are removed; nothing runs unless
dispatched by hand. The KILL file is in place and `trading_enabled` is
false, as they were throughout.

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
