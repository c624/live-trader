"""Slow Maker: a paper market maker on Kalshi's daily high-temperature brackets.

The question: can a small, slow bot earn the bid-ask spread on quiet markets, or do faster traders pick it off?
Nothing here can place an order. Each tick (about every five minutes, the wallet-watch loop):

1. Fills. For every quote posted last tick, Kalshi's public trade feed since then is read. A trade where the taker
   bought YES at or above our ask fills our ask; a taker selling YES at or below our bid fills our bid. Our quote
   sat a cent inside the best price, so it would have been first in line. Fills are capped at our size.
2. Settlement. Inventory in a market that has resolved is settled at $1 or $0.
3. Markouts. Each fill is marked against the mid about 5 and 60 minutes later: did the price move against us
   right after we traded (getting picked off) or not.
4. New quotes, replacing the old ones. On every open daily-high bracket whose best YES bid and ask are both
   3-97c and at least 3c apart: bid 1c above the best bid and offer 1c below the best ask, 10 contracts a side.
   A side that would take inventory past 50 contracts either way in that market is not quoted.

Costs: Kalshi's maker fee, 1.75% x P x (1 - P) per contract rounded up to the cent per fill, charged on every fill
(many markets charge makers nothing, so this is the cautious case).

The bar, fixed before the first fill: judged on 2026-12-15 or at 300 settled city-days with fills, whichever comes
first. It passes only if the 95% interval of P&L per filled contract (spread, markouts, fees and settled inventory
together), clustered by city-day, lies entirely above zero.

State in STATE_DIR/maker/: quotes.json, fills.csv, positions.json, meta.json, summary.md.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import pathlib
import time

from polymarket_watch import poly
from polymarket_watch.kalshi import BASE, MONTHS, Kalshi
from weather_watch.watch import ET, UTC, discover, num

SIZE = 10
MAX_INV = 50
TICK = 0.01
MIN_GAP = 0.03
EDGE = (0.03, 0.97)
MAKER_RATE = 0.0175
MARKOUTS = (5 * 60, 60 * 60)
BAR_DAYS = 300
BAR_DATE = dt.date(2026, 12, 15)
SERIES_REFRESH = 24 * 3600
FILL_FIELDS = ["id", "ts", "at", "ticker", "city", "day", "side", "price", "qty", "fee", "mid", "mid_5m", "mid_60m"]


def maker_fee(price: float, qty: int) -> float:
    return math.ceil(round(MAKER_RATE * qty * price * (1 - price) * 100, 6)) / 100


def day_of(event_ticker: str) -> str:
    """'KXHIGHNY-26OCT22' -> '2026-10-22'."""
    code = str(event_ticker).rsplit("-", 1)[-1]
    try:
        return dt.date(2000 + int(code[:2]), MONTHS.index(code[2:5].upper()) + 1, int(code[5:7])).isoformat()
    except (ValueError, IndexError):
        return ""


def quote_for(m: dict, inv: int) -> dict | None:
    bid, ask = num(m.get("yes_bid_dollars")), num(m.get("yes_ask_dollars"))
    if bid is None or ask is None or ask - bid < MIN_GAP - 1e-9:
        return None
    if not (EDGE[0] - 1e-9 <= bid and ask <= EDGE[1] + 1e-9):
        return None
    q = {"bid": round(bid + TICK, 2) if inv + SIZE <= MAX_INV else None,
         "ask": round(ask - TICK, 2) if inv - SIZE >= -MAX_INV else None,
         "mid": round((bid + ask) / 2, 4), "left_bid": SIZE, "left_ask": SIZE}
    return q if q["bid"] is not None or q["ask"] is not None else None


def trades_since(http: poly.Http, ticker: str, since: int, until: int) -> list[dict]:
    out, cursor = [], None
    for _ in range(10):
        page = http.get(f"{BASE}/markets/trades", {"ticker": ticker, "min_ts": since, "max_ts": until, "limit": 1000,
                                                    **({"cursor": cursor} if cursor else {})}) or {}
        out += page.get("trades") or []
        cursor = page.get("cursor")
        if not cursor or not page.get("trades"):
            break
    rows = []
    for t in out:
        price = num(t.get("yes_price_dollars"))
        if price is None and num(t.get("yes_price")) is not None:
            price = num(t.get("yes_price")) / 100
        qty = int(num(t.get("count_fp")) or num(t.get("count")) or 0)  # whole contracts; a fractional 0.5 fills none
        when = poly.parse_time(str(t.get("created_time"))).timestamp() if t.get("created_time") else since
        if price is not None and qty > 0:
            rows.append({"price": price, "qty": qty, "taker": str(t.get("taker_side") or ""), "ts": int(when),
                         "id": str(t.get("trade_id") or "")})
    return sorted(rows, key=lambda r: (r["ts"], r["id"]))


def match(q: dict, trades: list[dict]) -> list[dict]:
    """Our fills from one quote against the trades that printed while it stood."""
    fills = []
    for t in trades:
        if t["taker"] == "yes" and q.get("ask") is not None and q["left_ask"] > 0 and t["price"] >= q["ask"] - 1e-9:
            n = min(t["qty"], q["left_ask"])
            q["left_ask"] -= n
            fills.append({"side": "sell", "price": q["ask"], "qty": n, "ts": t["ts"], "trade": t["id"]})
        elif t["taker"] == "no" and q.get("bid") is not None and q["left_bid"] > 0 and t["price"] <= q["bid"] + 1e-9:
            n = min(t["qty"], q["left_bid"])
            q["left_bid"] -= n
            fills.append({"side": "buy", "price": q["bid"], "qty": n, "ts": t["ts"], "trade": t["id"]})
    return fills


def load(state: pathlib.Path):
    def js(name, default):
        p = state / name
        return json.loads(p.read_text()) if p.exists() else default
    fills = []
    if (state / "fills.csv").exists():
        with (state / "fills.csv").open(newline="") as f:
            fills = list(csv.DictReader(f))
    return js("quotes.json", {}), fills, js("positions.json", {}), js("meta.json", {})


def save(state: pathlib.Path, quotes, fills, positions, meta) -> None:
    state.mkdir(parents=True, exist_ok=True)
    (state / "quotes.json").write_text(json.dumps(quotes, indent=1, sort_keys=True))
    (state / "positions.json").write_text(json.dumps(positions, indent=1, sort_keys=True))
    (state / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True))
    with (state / "fills.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, FILL_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(fills)


def tick(state: pathlib.Path, http: poly.Http, now: int) -> dict:
    quotes, fills, positions, meta = load(state)
    meta.setdefault("started", dt.datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"))
    notes = []
    if now - meta.get("series_at", 0) > SERIES_REFRESH or not meta.get("series"):
        try:
            found = discover(http)
            if found:
                meta["series"], meta["series_at"] = found, now
        except Exception as e:
            notes.append(f"series discovery failed: {str(e)[:160]}")
    k = Kalshi(http)
    book = {}
    for ticker, city in sorted((meta.get("series") or {}).items()):
        for m in k.series(ticker):
            book[m["ticker"]] = m | {"_city": city, "_day": day_of(m.get("event_ticker") or m["ticker"])}

    # 1. fills against last tick's quotes
    new = 0
    for ticker, q in quotes.items():
        try:
            trades = trades_since(http, ticker, int(q["ts"]) + 1, now)
        except Exception as e:
            notes.append(f"{ticker} trades: {str(e)[:120]}")
            continue
        for f in match(q, trades):
            p = positions.setdefault(ticker, {"inv": 0, "cash": 0.0, "city": q["city"], "day": q["day"], "contracts": 0})
            fee = maker_fee(f["price"], f["qty"])
            sign = 1 if f["side"] == "buy" else -1
            p["inv"] += sign * f["qty"]
            p["cash"] = round(p["cash"] - sign * f["price"] * f["qty"] - fee, 4)
            p["contracts"] += f["qty"]
            fills.append({"id": f"{ticker}:{f['trade']}:{f['side']}", "ts": f["ts"],
                          "at": dt.datetime.fromtimestamp(f["ts"], UTC).isoformat(timespec="seconds"), "ticker": ticker,
                          "city": q["city"], "day": q["day"], "side": f["side"], "price": f["price"], "qty": f["qty"],
                          "fee": fee, "mid": q["mid"], "mid_5m": "", "mid_60m": ""})
            new += 1

    # 2. settle resolved inventory
    for ticker, p in positions.items():
        if p.get("settled") or ticker in book:
            continue
        try:
            m = k.market(ticker)
        except Exception as e:
            notes.append(f"{ticker} settle: {str(e)[:120]}")
            continue
        if m.get("result") in ("yes", "no"):
            p["cash"] = round(p["cash"] + p["inv"] * (1.0 if m["result"] == "yes" else 0.0), 4)
            p |= {"settled": dt.datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"), "result": m["result"],
                  "held": p["inv"], "inv": 0}

    # 3. markouts
    for f in fills:
        m = book.get(f["ticker"])
        if not m:
            continue
        b, a = num(m.get("yes_bid_dollars")), num(m.get("yes_ask_dollars"))
        if b is None or a is None:
            continue
        for col, after in zip(("mid_5m", "mid_60m"), MARKOUTS):
            if f[col] == "" and now - int(f["ts"]) >= after:
                f[col] = round((b + a) / 2, 4)

    # 4. new quotes
    quotes = {}
    for ticker, m in book.items():
        q = quote_for(m, positions.get(ticker, {}).get("inv", 0))
        if q:
            quotes[ticker] = q | {"ts": now, "city": m["_city"], "day": m["_day"]}
    meta |= {"ticks": meta.get("ticks", 0) + 1, "last_tick": dt.datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
             "quoted": len(quotes)}
    save(state, quotes, fills, positions, meta)
    (state / "summary.md").write_text(report(fills, positions, meta, now))
    return {"fills": new, "quoted": len(quotes), "notes": notes}


def clustered(groups: list[tuple[float, float]]) -> dict | None:
    """Ratio estimate of P&L per contract (cents) over (pnl, contracts) clusters, with a 95% interval."""
    groups = [g for g in groups if g[1] > 0]
    if not groups:
        return None
    tot_c = sum(c for _, c in groups)
    mean = sum(p for p, _ in groups) / tot_c
    g = len(groups)
    if g < 2:
        return {"n": tot_c, "g": g, "mean": mean * 100, "lo": None, "hi": None}
    var = sum((p - mean * c) ** 2 for p, c in groups) / tot_c ** 2 * g / (g - 1)
    half = 1.96 * math.sqrt(var)
    return {"n": tot_c, "g": g, "mean": mean * 100, "lo": (mean - half) * 100, "hi": (mean + half) * 100}


def report(fills: list[dict], positions: dict, meta: dict, now: int) -> str:
    today = dt.datetime.fromtimestamp(now, UTC).astimezone(ET).date()
    days: dict[tuple, list] = {}
    for t, p in positions.items():
        days.setdefault((p["city"], p["day"]), []).append(p)
    settled_days = [(sum(p["cash"] for p in ps), sum(p["contracts"] for p in ps)) for ps in days.values()
                    if all(p.get("settled") for p in ps)]
    s = clustered(settled_days)
    due = (s and s["g"] >= BAR_DAYS) or today >= BAR_DATE
    if not due:
        verdict = f"Running: {s['g'] if s else 0} of {BAR_DAYS} settled city-days (or judged on {BAR_DATE}). Not judged yet."
    elif s and s["lo"] is not None and s["lo"] > 0:
        verdict = f"**Passed**: {s['lo']:+.2f}c to {s['hi']:+.2f}c per contract lies above zero."
    else:
        verdict = "**Failed**: the 95% interval does not lie above zero."
    qty = sum(int(f["qty"]) for f in fills)
    def per(col):
        rows = [f for f in fills if f[col] != ""]
        n = sum(int(f["qty"]) for f in rows)
        if not n:
            return "n/a"
        v = sum(int(f["qty"]) * (float(f[col]) - float(f["price"])) * (1 if f["side"] == "buy" else -1) for f in rows)
        return f"{v / n * 100:+.2f}c over {n}"
    spread = sum(int(f["qty"]) * (float(f["mid"]) - float(f["price"])) * (1 if f["side"] == "buy" else -1) for f in fills)
    fees = sum(float(f["fee"]) for f in fills)
    mean = f"{s['mean']:+.2f}c" if s else "n/a"
    band = f"{s['lo']:+.2f}c to {s['hi']:+.2f}c" if s and s["lo"] is not None else "n/a"
    open_inv = sum(abs(p["inv"]) for p in positions.values() if not p.get("settled"))
    lines = ["# Slow Maker: paper market making on weather brackets", "",
             f"Started {meta.get('started')}; tick {meta.get('ticks')}, {meta.get('quoted', 0)} markets quoted now. "
             f"Quotes 1c inside the best bid and ask, {SIZE} a side, inventory capped at {MAX_INV} per market, "
             f"refreshed about every 5 minutes; maker fee charged on every fill.", "",
             f"**Verdict:** {verdict}", "",
             "| Fills | Contracts | Spread earned | Fees | Settled city-days | P&L per contract (settled) | 95% interval | Open inventory |",
             "|---|---|---|---|---|---|---|---|",
             f"| {len(fills)} | {qty} | ${spread:+,.2f} | ${fees:,.2f} | {s['g'] if s else 0} | {mean} | {band} | {open_inv} |", "",
             f"Markouts (mid after the fill against our price, per contract; negative means picked off): "
             f"5 min {per('mid_5m')}, 60 min {per('mid_60m')}.", "",
             f"Settled P&L so far: ${sum(p for p, _ in settled_days):+,.2f}.", ""]
    return "\n".join(lines) + "\n"


def main() -> None:
    state = pathlib.Path(os.environ.get("STATE_DIR", "out")) / "maker"
    http = poly.Http()
    res = tick(state, http, int(time.time()))
    print(f"maker: {res['fills']} new fills, {res['quoted']} markets quoted, {http.count} requests")
    for n in res["notes"]:
        print("note:", n)


if __name__ == "__main__":
    main()
