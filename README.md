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

### The wash-trade study (closed 2026-09-05)

Carter's hypothesis: the aggregates cannot tell a pump from a dump because
inflated buying is built to look like buying; the difference should show
one level down, in how the transactions arrive. A retrospective study
(`washstudy.py`, results on the `wash-study` branch) read the five
minutes before entry from the chain for 545 of the ledger's hot tokens
(106 that doubled, 180 that went to zero, 259 in between) on free public
RPC: signature clustering into blocks, distinct buyers, size uniformity,
buyer wallet age.

| at entry (median) | pumps | dumps |
|---|---|---|
| share of transactions in same-block bursts | 35% | 67% |
| transactions in the five minutes | 1,230 | 1,520 |
| unevenness of arrival (CV of gaps) | 1.37 | 1.17 |
| busiest single buyer's share of buys | 5.9% | 6.9% |
| distinct buyers per buy | 0.97 | 0.97 |
| buyers with fewer than ten lifetime transactions | 0% | 0% |

The dumps are twice as bundled. That is the largest entry-time difference
between pumps and dumps found anywhere in this project, and it points the
way the hypothesis said. Split-sample, with the cut chosen on the first
272 tokens by entry time and judged on the last 273: tokens under the
bundling cut averaged +30% (interval -9% to +69%) against -10% for the
rest, but their pump rate (25%) did not clear their dump rate (27%), and
the interval spans zero. Under the bar fixed before the run, that is a
fail. Every buyer wallet had ten or more transactions already: this is a
market of bots trading with bots, and wallet freshness says nothing.

What it means: bundling is the first signal in 17,000 trades that moved
the out-of-sample mean in the predicted direction, and it is not proof.
It would need a forward paper arm (hot tokens with low bundling against
hot tokens regardless) run to 100 trades each under the usual bar. The
loop is stopped and stays stopped unless Carter asks for that test.

### The bundling test (closed 2026-09-07)

Carter asked for the forward test. Four paper arms on the hot population
(311 or more buys in the five minutes before entry, 120 seconds to two
hours old), all of which require the bundling read to have succeeded so
the arm and its control draw from the same tokens:

- `organic`: share of the last five minutes' transactions in same-block
  bursts at or below 0.25, ten-minute hold.
- `hotctl`: the same population with no bundling rule, ten-minute hold.
- `organic1h` and `hotctl1h`: the same pair with a one-hour hold.

The bundling share is read at entry from the mint's newest thousand
signatures (`chain.bundling`), the same page the traction read already
fetches. The threshold 0.25 comes from the retrospective study, which
measured the full five-minute window rather than the newest thousand
signatures, so the two readings are close but not identical; the rule is
fixed here before the first trade regardless.

The bar, fixed before the first trade: an organic arm passes only with
100 or more closed trades AND a 95% interval lying entirely above its
control's. The two holds are judged separately. Anything short of that
closes the bundling line of work.

Paper only. The KILL file is in place and `trading_enabled` is false.

Addendum, 04:15 UTC on 2026-09-07, ninety minutes in: the one-hour control
was hitting the default cap of twenty open positions and skipping
candidates its organic partner still took, which would have drawn the
two arms from different tokens. Every arm's cap is raised to sixty. This
changes capacity, not selection, and is recorded here because it is a
change after the first trade.

Result, 12:23 UTC on 2026-09-07, ten hours in, both organic arms past 100:

| arm | hold | n | mean | median | went to zero | doubled | 95% interval |
|---|---|---|---|---|---|---|---|
| organic | 10 min | 110 | +2.5% | +0.7% | 15% | 16% | -10.8% to +15.9% |
| hotctl | 10 min | 463 | +0.8% | -7.0% | 28% | 19% | -10.1% to +11.7% |
| organic1h | 1 hour | 103 | -3.3% | -24.3% | 27% | 24% | -20.2% to +13.7% |
| hotctl1h | 1 hour | 383 | -4.2% | -66.9% | 42% | 30% | -17.2% to +8.8% |

Both pairs fail the bar: neither organic interval lies above its
control's, and both span zero.

What did hold, forward and out of sample: the bundling filter halves the
share of entries that go to zero, at both holds. That is the finding the
retrospective study predicted, and it is real. It does not pay, because
the same filter drops as many of the coins that double, and what remains
is mostly trades that end within a few percent of where they started,
paying the round trip for nothing. Without its three best trades the
ten-minute organic arm reads -1.8%, the fee floor again.

That closes the bundling line, and with it the last forward test this
project had. The loop's schedule and self-chain are removed; nothing runs
unless dispatched by hand. The KILL file is in place and `trading_enabled`
is false.

### The wider wallet scan (opened 2026-09-07)

The August wallet study nominated wallets from 64 winners and found
sprayers with an edge under the cost of copying them. The record now holds
9,600 coins with a measured outcome, 485 of which doubled from the bot's
own quoted entry, so the question is asked again over eight times the
winners, with a control the first pass lacked: 300 coins that lost 80% or
more, read the same way. A wallet early in winners and not in dumps is
selecting; a wallet early in both is buying everything.

`wallet_pnl/src/widescan.py`, run by `wallet-scan.yml` on free public RPC,
no key and no trades. For each coin the signature history is walked back
from the present to the half hour before the bot's entry (skipped and
reported if that takes more than 40 pages), 80 transactions spread over
that window are parsed, and every wallet that spent SOL and received the
token is recorded. Today's GeckoTerminal runners (up 200%+ on the day) are
added as a supplement; the free feeds keep no history, so the record is
the body of the sample. Wallets early in three or more distinct winners
are graded on their own most recent thousand transactions by the honest
ledger, open bags written to zero.

**Pre-registered bar, fixed before the first run.** A wallet passes when
it bought at least 20 distinct tokens in the graded window, at least 10%
of them are winners from our record, its median hold on closed positions
is five minutes or longer, and its honest return on SOL deployed is +10%
or better. One or more passes justifies a forward paper test of following
that wallet. None closes the copy-trade line on the widest set of winners
the project can measure. Results land on the `wallet-scan` branch as
`REPORT.txt`.

**Run 1, 13:10 to 14:12 UTC: no wallet passes, and the window was wrong.**
775 coins read (475 winners, 300 dumps, 22 busy winners skipped at the
page budget), 9,238 early buys by 6,878 wallets, 92 wallets early in three
or more winners, the top 40 graded. None passed. But the window stopped at
the bot's own entry, and most of the record was bought at birth, when
nobody had bought yet: 339 of 475 winners and 270 of 300 dumps had no
buyers to read, so the dump control was empty and every nominee scored a
perfect selection ratio by default. What was read looked like August:
wallets that buy everything at 1 to 3 SOL and lose the fee (honest -1% to
-4%), dust sprayers at thousands of tokens a day, and two fast bots whose
thousand signatures covered under two hours. Run 2 widens the window to
five minutes after the entry, which is where the August study read its
early buyers, with the bar unchanged.

**Run 3, 17:52 to 02:22 UTC on the history endpoint: full coverage, one
wallet clears the bar, and it does not look like a trader.** The Solana
Foundation endpoint (api.mainnet-beta.solana.com) was the only public one
that still answered with September 1 history when probed; publicnode
keeps about a day, which is why runs 1 and 2 saw nothing on 608 coins.
Read with it alone and no fallback: 735 coins (441 winners, 294 dumps, 66
busy winners skipped at the page budget), a median of 1,995 signatures in
each winner's window and 972 in each dump's, 37,582 early buys by 22,772
wallets, 811 wallets early in three or more winners, the top 40 graded.

| wallet | wins | dumps | tokens | hit | win rate | honest | hold | buy | history |
|---|---|---|---|---|---|---|---|---|---|
| B1Ec85Nh | 7 | 0 | 34 | 21% | 100% | **+53.6%** | 7.5 min | 71.5 SOL | 6 h |
| NMVXLGSV | 10 | 1 | 14 | 71% | 100% | +285% | 118 min | 43.3 SOL | 10 h |
| EPnbFpE1 | 7 | 0 | 17 | 41% | 100% | +102% | 40 min | 5.0 SOL | 5 h |
| 64hP97Bw | 20 | 1 | 161 | 12% | 39% | +4.3% | 3.4 min | 0.94 SOL | 4 h |
| omegoMAe | 15 | 0 | 118 | 13% | 44% | -2.2% | 3.7 min | 0.44 SOL | 2 h |
| BwWK17cb | 12 | 36 | 18 | 67% | 91% | -40.7% | 1.0 min | 0.03 SOL | 5 min |

B1Ec85Nh passes every clause of the bar. But its early buys on our seven
winners were all 0.02 SOL probes, placed two to ten minutes before the
bot's own entry on coins with thousands of transactions in that window,
while its graded history shows a 71.5 SOL median buy and a 100% win rate
on 34 coins inside six hours. That is not a person picking coins. It is
either a whale whose own buy is the pump, a program account the parser
mistook for a trader, or a bot whose legs are not trades a copier could
place behind it. The same shape sits on the two rows above it. The rest
of the table is August again: buy-everything wallets at 1 to 3 SOL losing
the fee, and the August sprayers (omegoMAe, BwWK17cb) still at or under
it. The passing wallet is being inspected on-chain before anything is
built on it.

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
