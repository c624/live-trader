"""The live loop: detect, buy, monitor, exit. Everything dangerous is behind
three gates: config trading_enabled, a wallet key actually present, and the
KILL / KILL_ALL files. With any gate shut this is a signal logger.
"""

from __future__ import annotations

import os
import time

from .chain import Rpc, load_keypair
from .config import kill_switch, load_config
from .decisions import exit_reason, parse_created, pick_entries, utc_day
from .gecko import Gecko
from .jupiter import SOL_MINT, Jupiter, price_impact_pct
from .notify import send
from .state import load_state, log_signal, log_trade, save_state, use_paper_state

LAMPORTS = 1_000_000_000

# Qualified signals get a ledger row; unqualified pool listings do not, so
# signals.csv stays one row per unique candidate, not a firehose.
LOGGED_SKIPS = {
    "loop_cap", "position_cap", "daily_spend_cap", "already_held",
    "no_route", "impact_too_high", "wallet_low",
    # One in nine launches could be bought but not sold. A skip for that has
    # to be visible, or the filter looks like the market simply went quiet.
    "no_exit_route",
    # A cap that silently stops all buying has to say so. The pilot's loss
    # cap blocked an entire run while the logs showed only "0 buys".
    "loss_cap", "too_old", "too_young", "missing_data", "clock_skew",
}


def now() -> float:
    return time.time()


def _signal_row(row: dict, decision: str, reason: str, ts: float) -> dict:
    created = parse_created(row.get("pool_created_at"))
    return {
        "ts": int(ts),
        "mint": row["token"],
        "symbol": row.get("symbol", "?"),
        "pool": row.get("pool", ""),
        "age_hours": round((ts - created) / 3600, 3) if created else "",
        "reserve_usd": row.get("reserve_usd") or "",
        "gecko_price_usd": row.get("price_usd") or "",
        "decision": decision,
        "reason": reason,
    }


class Trader:
    def __init__(self):
        self.cfg = load_config()
        self.keypair = load_keypair()
        # Paper runs keep their own ledger. Sharing one would let simulated
        # fills feed the loss cap and the daily spend cap that govern real
        # money, and would leave paper positions sitting in live state if the
        # bot were later armed. A separate directory makes that impossible
        # rather than merely unlikely.
        if not self.live:
            use_paper_state()
        self.state = load_state()
        self.jup = Jupiter()
        self.rpc = Rpc()
        self.gecko = Gecko()
        self.feed = None          # launch subscription, started in run()
        self.sol_price: float | None = None
        self.wallet_low = False

    @property
    def live(self) -> bool:
        # Deliberately no key check. This used to require HELIUS_API_KEY, which
        # became a trap the moment the RPC layer stopped needing one: an armed
        # bot with no key would have reported itself live-capable as "dry run"
        # and quietly never traded. What actually gates trading is the config
        # flag and a signing key -- the endpoints are keyless now.
        return bool(self.cfg["trading_enabled"] and self.keypair is not None)

    @property
    def pubkey(self) -> str:
        return str(self.keypair.pubkey())

    def open_positions(self) -> list[dict]:
        return [p for p in self.state["positions"] if p.get("status") in ("open", "pending")]

    # ------------------------------------------------------------- buying
    def detect_and_buy(self, ts: float) -> None:
        # The subscription is the discovery path when it is running:
        # GeckoTerminal lists a pool 160 seconds after its first trade and
        # the edge is gone by 60, so polling it is a slow way to lose. The
        # poller stays as the fallback for a dead socket.
        # A feed that exists but delivers nothing is worse than no feed: the
        # first armed cycle ran blind for twenty-five minutes because a dead
        # socket looked exactly like a quiet market.
        if self.feed is not None and self.feed.healthy:
            rows = self.feed.drain()
            source = "feed"
        else:
            rows = self.gecko.candidate_pools()
            source = "gecko(feed down)" if self.feed is not None else "gecko"
        buys, skipped = pick_entries(rows, self.state, ts, self.cfg)
        # One line per detection so a silent market is distinguishable from
        # a blind detector in the run logs.
        ages = [ts - float(r["first_trade_ts"]) for r in rows if r.get("first_trade_ts")]
        age_note = f", ages {min(ages):.0f}-{max(ages):.0f}s" if ages else ""
        print(f"detect[{source}]: {len(rows)} rows, {len(buys)} buys, "
              f"{len(skipped)} skipped{age_note}")
        for row, reason in skipped:
            if reason in LOGGED_SKIPS:
                log_signal(_signal_row(row, "skip", reason, ts))
        kill = kill_switch()
        for row in buys:
            if not self.live:
                self.paper_buy(row, ts)
                continue
            if kill:
                log_signal(_signal_row(row, "skip", f"kill_switch_{kill}", ts))
                continue
            if self.wallet_low:
                log_signal(_signal_row(row, "skip", "wallet_low", ts))
                continue
            self.buy(row, ts)

    def buy(self, row: dict, ts: float) -> None:
        mint, symbol = row["token"], row.get("symbol", "?")
        if not self.sol_price:
            log_signal(_signal_row(row, "skip", "no_sol_price", ts))
            return
        lamports = int(self.cfg["ticket_usd"] / self.sol_price * LAMPORTS)
        balance = self.rpc.sol_balance_lamports(self.pubkey)
        reserve = int(self.cfg["min_wallet_reserve_sol"] * LAMPORTS)
        if balance is None or balance < lamports + reserve:
            self.wallet_low = True
            log_signal(_signal_row(row, "skip", "wallet_low", ts))
            send(f"Wallet balance too low to buy ({(balance or 0) / LAMPORTS:.4f} SOL). Buys paused.")
            return
        quote = self.jup.quote(SOL_MINT, mint, lamports, self.cfg["slippage_bps"])
        if not quote:
            log_signal(_signal_row(row, "skip", "no_route", ts))
            return
        impact = price_impact_pct(quote)
        if impact is not None and impact > self.cfg["max_price_impact_pct"]:
            log_signal(_signal_row(row, "skip", "impact_too_high", ts))
            return

        # Check the way out before taking the way in. Measured on 279 live
        # launches, 31 of them -- about one in nine -- could be bought at
        # thirty seconds old and had no sell route at all. Buying one of those
        # is a position that cannot be exited, roughly a total loss, and at
        # that rate it wipes out the entire modelled edge on its own. A quote
        # costs about 66ms against an entry budget of tens of seconds, so
        # there is no reason not to ask first.
        if not self.jup.quote(mint, SOL_MINT, int(quote["outAmount"]),
                              self.cfg["slippage_bps"]):
            log_signal(_signal_row(row, "skip", "no_exit_route", ts))
            return

        tx = self.jup.swap_transaction(quote, self.pubkey)
        signature = self.rpc.sign_and_send(tx, self.keypair) if tx else None
        if not signature:
            log_signal(_signal_row(row, "skip", "send_failed", ts))
            return

        # The position is recorded as pending BEFORE confirmation so that a
        # crash between send and confirm cannot orphan bought tokens.
        position = {
            "mint": mint,
            "symbol": symbol,
            "pool": row.get("pool", ""),
            "status": "pending",
            "opened_ts": ts,
            "sol_lamports": lamports,
            "cost_usd": round(lamports / LAMPORTS * self.sol_price, 4),
            "entry_gecko_price_usd": row.get("price_usd"),
            "first_trade_ts": row.get("first_trade_ts"),
            "entry_lag_s": (
                round(ts - float(row["first_trade_ts"]), 1)
                if row.get("first_trade_ts") else None
            ),
            "token_raw": 0,
            "peak_usd": 0.0,
            "signature": signature,
        }
        self.state["positions"].append(position)
        day = utc_day(ts)
        self.state["daily_spend"][day] = self.state["daily_spend"].get(day, 0.0) + position["cost_usd"]
        save_state(self.state)

        confirmed = self.rpc.confirm(signature)
        token_raw = (
            self.rpc.wait_token_balance(self.pubkey, mint)
            if confirmed
            else self.rpc.token_balance_raw(self.pubkey, mint)
        )
        if token_raw <= 0 and confirmed:
            # Confirmed but tokens not visible yet: keep the position pending
            # so the reconciler re-checks it on later loops instead of either
            # abandoning real tokens or double-closing.
            save_state(self.state)
            send(f"{symbol} buy confirmed but tokens not visible yet; reconciling next loop.")
            return
        if token_raw <= 0 and not confirmed:
            position["status"] = "closed"
            position["close_reason"] = "buy_failed"
            self.state["daily_spend"][day] -= position["cost_usd"]
            log_trade({"ts": int(ts), "action": "buy_failed", "mint": mint, "symbol": symbol,
                       "pool": position["pool"], "signature": signature,
                       "note": "unconfirmed, no tokens"})
            save_state(self.state)
            return
        position["status"] = "open"
        position["token_raw"] = token_raw
        log_signal(_signal_row(row, "buy", "", ts))
        log_trade({"ts": int(ts), "action": "buy", "mint": mint, "symbol": symbol,
                   "pool": position["pool"], "sol_lamports": lamports, "token_raw": token_raw,
                   "usd_value": position["cost_usd"], "gecko_price_usd": row.get("price_usd") or "",
                   "signature": signature})
        save_state(self.state)
        send(f"Bought {symbol} for ${position['cost_usd']:.2f} "
             f"(TP +{self.cfg['exit']['tp_pct']}% / SL -{self.cfg['exit']['sl_pct']}% "
             f"/ {self.cfg['exit']['hold_hours']}h hold)")

    def paper_buy(self, row: dict, ts: float) -> None:
        """Take the position on paper, priced by real quotes.

        Dry run used to log "would_buy" and stop, which records an intention
        and no outcome -- so a paper run could never say whether the strategy
        made money. That matters more than it sounds: a $30 pilot buys 15
        trades, and against a per-trade spread of roughly 50 points, 15 trades
        cannot distinguish a +8.7% edge from nothing. Paper trades are free,
        so the edge question is answered here and the pilot is left to answer
        only whether transactions land.

        Same feed, same filters, same quotes as the live path. The only thing
        missing is the send, which is the one part that costs money.
        """
        mint, symbol = row["token"], row.get("symbol", "?")
        if not self.sol_price:
            log_signal(_signal_row(row, "skip", "no_sol_price", ts))
            return
        lamports = int(self.cfg["ticket_usd"] / self.sol_price * LAMPORTS)
        quote = self.jup.quote(SOL_MINT, mint, lamports, self.cfg["slippage_bps"])
        if not quote:
            log_signal(_signal_row(row, "skip", "no_route", ts))
            return
        impact = price_impact_pct(quote)
        if impact is not None and impact > self.cfg["max_price_impact_pct"]:
            log_signal(_signal_row(row, "skip", "impact_too_high", ts))
            return
        tokens = int(quote["outAmount"])
        # The same exit check the live path makes, so the paper record is not
        # flattered by entries the real bot would have refused.
        if not self.jup.quote(mint, SOL_MINT, tokens, self.cfg["slippage_bps"]):
            log_signal(_signal_row(row, "skip", "no_exit_route", ts))
            return

        self.state["positions"].append({
            "mint": mint,
            "symbol": symbol,
            "pool": row.get("pool", ""),
            "status": "open",
            "paper": True,
            "opened_ts": ts,
            "token_raw": tokens,
            "cost_usd": self.cfg["ticket_usd"],
            "peak_usd": self.cfg["ticket_usd"],
            "entry_lag_s": round(ts - float(row["first_trade_ts"]), 1)
            if row.get("first_trade_ts") else "",
        })
        log_signal(_signal_row(row, "paper_buy", "", ts))
        log_trade({"ts": int(ts), "action": "paper_buy", "mint": mint,
                   "symbol": symbol, "pool": row.get("pool", ""),
                   "sol_lamports": lamports, "token_raw": tokens,
                   "usd_value": self.cfg["ticket_usd"],
                   "gecko_price_usd": row.get("price_usd") or "",
                   "note": "paper"})
        save_state(self.state)

    def paper_close(self, position: dict, value_usd: float | None,
                    reason: str, ts: float) -> None:
        """Close at the quoted price. No route means it is worth nothing."""
        proceeds = value_usd if value_usd is not None else 0.0
        position["status"] = "closed"
        position["close_reason"] = reason
        position["proceeds_usd"] = round(proceeds, 4)
        position["pnl_usd"] = round(proceeds - position["cost_usd"], 4)
        log_trade({"ts": int(ts), "action": "paper_sell", "mint": position["mint"],
                   "symbol": position["symbol"], "pool": position["pool"],
                   "token_raw": position.get("token_raw", 0),
                   "usd_value": round(proceeds, 4), "reason": reason,
                   "note": f"paper pnl {position['pnl_usd']:+.4f}"})

    # ------------------------------------------------------------ selling
    def check_exits(self, ts: float) -> None:
        kill = kill_switch()
        for position in list(self.open_positions()):
            if position["status"] == "pending":
                self._reconcile_pending(position, ts)
                continue
            token_raw = position.get("token_raw") or 0
            if token_raw <= 0:
                continue
            quote = self.jup.quote(position["mint"], SOL_MINT, token_raw, self.cfg["slippage_bps"])
            value_usd = None
            if quote and self.sol_price:
                value_usd = int(quote["outAmount"]) / LAMPORTS * self.sol_price
                position["no_route_checks"] = 0
                position["peak_usd"] = max(position.get("peak_usd", 0.0), value_usd)
                position["last_value_usd"] = round(value_usd, 4)
                position["value_stale"] = False
                position["valued_at"] = int(ts)
            else:
                # A failed quote means no sell route. The previous value is
                # now fiction, not a price: mark it stale so no reader
                # mistakes a frozen number for a live one.
                position["no_route_checks"] = position.get("no_route_checks", 0) + 1
                position["value_stale"] = True
            reason = exit_reason(position, value_usd, ts, self.cfg)
            if position.get("paper"):
                if reason:
                    self.paper_close(position, value_usd, reason, ts)
                save_state(self.state)
                continue
            if reason == "dead":
                position["status"] = "closed"
                position["close_reason"] = "dead"
                log_trade({"ts": int(ts), "action": "writeoff", "mint": position["mint"],
                           "symbol": position["symbol"], "pool": position["pool"],
                           "usd_value": 0, "reason": "no_route", "note": "liquidity gone"})
                send(f"{position['symbol']} written off: no sell route (likely rug). "
                     f"-${position['cost_usd']:.2f}")
            elif reason and kill != "KILL_ALL":
                self.sell(position, quote, value_usd, reason, ts)
            save_state(self.state)

    def _reconcile_pending(self, position: dict, ts: float) -> None:
        """A pending position survived a crash between send and confirm."""
        token_raw = self.rpc.token_balance_raw(self.pubkey, position["mint"])
        if token_raw > 0:
            position["status"] = "open"
            position["token_raw"] = token_raw
        elif ts - position["opened_ts"] > 600:
            position["status"] = "closed"
            position["close_reason"] = "buy_failed"
        save_state(self.state)

    def sell(self, position: dict, quote: dict, value_usd: float, reason: str, ts: float) -> None:
        tx = self.jup.swap_transaction(quote, self.pubkey)
        signature = self.rpc.sign_and_send(tx, self.keypair) if tx else None
        if not signature or not self.rpc.confirm(signature):
            remaining = self.rpc.token_balance_raw(self.pubkey, position["mint"])
            if remaining > 0:
                position["sell_attempts"] = position.get("sell_attempts", 0) + 1
                if position["sell_attempts"] == 3:
                    send(f"{position['symbol']} sell failing repeatedly ({reason}); still trying.")
                return
        pnl_pct = (value_usd / position["cost_usd"] - 1.0) * 100.0
        position["status"] = "closed"
        position["close_reason"] = reason
        position["closed_ts"] = ts
        position["exit_usd"] = round(value_usd, 4)
        log_trade({"ts": int(ts), "action": "sell", "mint": position["mint"],
                   "symbol": position["symbol"], "pool": position["pool"],
                   "token_raw": position["token_raw"], "usd_value": round(value_usd, 4),
                   "reason": reason, "signature": signature or "",
                   "note": f"pnl_pct={pnl_pct:.1f}"})
        save_state(self.state)
        send(f"Sold {position['symbol']} ({reason}) for ${value_usd:.2f}, {pnl_pct:+.1f}%")

    # --------------------------------------------------------------- loop
    def run(self) -> None:
        loop_minutes = float(os.environ.get("LOOP_MINUTES", self.cfg["loop_minutes"]))
        deadline = time.monotonic() + loop_minutes * 60
        self._start_feed()
        check_seconds = self.cfg["check_seconds"]
        mode = "LIVE" if self.live else "dry-run"
        print(f"live-trader starting: {mode}, {loop_minutes:.0f} min loop, "
              f"{len(self.open_positions())} open positions")
        check = 0
        while time.monotonic() < deadline:
            started = time.monotonic()
            ts = now()
            self.sol_price = self.jup.sol_usd() or self.sol_price
            try:
                if check % self.cfg["detect_every_n_checks"] == 0:
                    self.detect_and_buy(ts)
                if self.live:
                    self.check_exits(ts)
            except Exception as exc:  # one bad loop must not kill the job
                print(f"loop error: {exc!r}")
                send(f"live-trader loop error: {exc!r}")
            save_state(self.state)
            check += 1
            remaining = check_seconds - (time.monotonic() - started)
            # If a full check interval no longer fits before the deadline,
            # end the loop instead of busy-spinning through the tail.
            if time.monotonic() + max(remaining, 0.0) >= deadline:
                break
            if remaining > 0:
                time.sleep(remaining)
        save_state(self.state)
        if self.feed is not None:
            print(f"feed: {self.feed.messages} messages, {self.feed.launches} launches, "
                  f"{self.feed.errors} errors, {self.feed.throttled} throttled, "
                  f"{self.feed.skipped_busy} skipped")
            self.feed.stop()
        print(f"loop done: {check} checks, {len(self.open_positions())} open positions")

    def _start_feed(self) -> None:
        """Start the launch subscription, or carry on without it.

        A missing socket must not stop the loop: open positions still need
        their exits run, and the poller can still find something, however
        late. Discovery degrading is survivable, exits going unmanaged is not.
        """
        if os.environ.get("DISABLE_FEED"):
            print("launch feed: disabled, falling back to polling")
            return
        # A launch stream sends one message per launch. The old subscription
        # took every log on the program - about 38,000 a minute to find
        # thirty - and exhausted a month of RPC credits in eight minutes.
        # Discovery needs no API key at all now, which is the point: it can
        # no longer spend the budget the exits depend on.
        try:
            from .launchfeed import LaunchStream
            stream = LaunchStream()
            stream.start()
            self.feed = stream
            print(f"launch stream: subscribing to {stream.url}")
        except Exception as exc:
            print(f"launch stream unavailable ({exc!r}); falling back to polling")


def main() -> None:
    Trader().run()


if __name__ == "__main__":
    main()
