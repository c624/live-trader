"""Storm Chaser: a forward paper test of selling long shots on Kalshi's daily high-temperature brackets.

The research repo's backtest (tradingbotgpt, prediction_markets/weather.py) replicated a published rule: at 17:00
Eastern, when a bracket's YES bid is 1-4c, buy NO at the ask and hold to settlement. It made +1.19c per contract over
282 trades with 2 losses, but one half of its window had no trades and two losses can't pin down a loss rate. This
is the forward test, paper only: nothing here can place an order.

The rule, fixed before the first trade:
- once a day, on the first tick from 17:00 to 17:30 Eastern, every open daily-high bracket (every KXHIGH* daily
  series) for that day whose YES bid is 1-4c, with a YES ask on the book and a spread of 5c or less;
- buy 100 NO contracts at the NO ask (1 - YES bid when Kalshi omits it), paying the taker fee of 7% x P x (1 - P)
  per contract, rounded up to the cent per order, and hold to settlement;
- a day whose window the loop misses is skipped and logged, never made up later.

The bar: judged at 1,000 settled trades or on 2027-01-31, whichever comes first. It passes only if the 95% interval
of the mean P&L per contract lies entirely above zero, with trades clustered by city-day (one hot day can sink every
bracket in a city at once). Anything short of that closes the line.

State in STATE_DIR/weather/: trades.csv, meta.json, summary.md.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import os
import pathlib
import re
import time
from zoneinfo import ZoneInfo

from polymarket_watch import poly
from polymarket_watch.kalshi import BASE, Kalshi, date_code

ET = ZoneInfo("America/New_York")
UTC = dt.timezone.utc
WINDOW = (dt.time(17, 0), dt.time(17, 30))  # Eastern; the backtest's check time and the slack a late tick gets
BAND = (0.01, 0.04)   # YES bid, dollars
MAX_SPREAD = 0.05
CONTRACTS = 100
BAR_TRADES = 1000
BAR_DATE = dt.date(2027, 1, 31)
BACKTEST = {"mean_c": 1.19, "lo_c": 0.37, "hi_c": 1.90, "trades": 282, "losses": 2}
SERIES_REFRESH = 24 * 3600
TRADE_FIELDS = ["ticker", "series", "city", "day", "bracket", "opened", "yes_bid", "yes_ask", "no_ask", "fee",
                "cost", "volume", "status", "result", "settled", "pnl", "pnl_c"]


def num(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def order_fee(price: float, contracts: int = CONTRACTS) -> float:
    """Kalshi's taker fee for one order in dollars: 7% x P x (1 - P) per contract, rounded up to the cent."""
    return math.ceil(round(0.07 * contracts * price * (1 - price) * 100, 6)) / 100


def city_of(series: dict) -> str:
    title = str(series.get("title") or "")
    hit = re.match(r"(?:Highest temperature in |High temp(?:erature)? in )?(.+?)(?: Daily Max Temp| daily| today|\?|$)",
                   title, re.I)
    return (hit.group(1).strip() if hit and hit.group(1).strip() else series.get("ticker", ""))


def discover(http: poly.Http) -> dict[str, str]:
    """Daily-high series ticker -> city, from Kalshi's weather category."""
    found = {}
    for s in (http.get(f"{BASE}/series", {"category": "Climate and Weather"}) or {}).get("series") or []:
        t = str(s.get("ticker", ""))
        if t.startswith("KXHIGH") and str(s.get("frequency") or "daily").lower() == "daily":
            found[t] = city_of(s)
    return found


def candidates(markets: list[dict], day: dt.date) -> list[dict]:
    """Brackets settling on `day` that the rule sells right now."""
    out = []
    for m in markets:
        if f"-{date_code(day)}" not in str(m.get("event_ticker") or m.get("ticker") or ""):
            continue
        bid, ask = num(m.get("yes_bid_dollars")), num(m.get("yes_ask_dollars"))
        if bid is None or ask is None or ask >= 1 or ask - bid > MAX_SPREAD + 1e-9:
            continue
        if not BAND[0] - 1e-9 <= bid <= BAND[1] + 1e-9:
            continue
        no_ask = num(m.get("no_ask_dollars"))
        if no_ask is None or no_ask <= 0 or no_ask >= 1:
            no_ask = round(1 - bid, 4)
        out.append({"market": m, "yes_bid": bid, "yes_ask": ask, "no_ask": no_ask})
    return out


def load(state: pathlib.Path) -> tuple[dict[str, dict], dict]:
    trades = {}
    if (state / "trades.csv").exists():
        with (state / "trades.csv").open(newline="") as f:
            trades = {t["ticker"]: t for t in csv.DictReader(f)}
    meta = json.loads((state / "meta.json").read_text()) if (state / "meta.json").exists() else {}
    return trades, meta


def save(state: pathlib.Path, trades: dict[str, dict], meta: dict) -> None:
    state.mkdir(parents=True, exist_ok=True)
    with (state / "trades.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, TRADE_FIELDS)
        w.writeheader()
        w.writerows(sorted(trades.values(), key=lambda t: (t["day"], t["ticker"])))
    (state / "meta.json").write_text(json.dumps(meta, indent=1, sort_keys=True))


def tick(state: pathlib.Path, http: poly.Http, now: int) -> dict:
    trades, meta = load(state)
    meta.setdefault("started", dt.datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"))
    meta.setdefault("scanned", {})
    notes, opened = [], []
    et = dt.datetime.fromtimestamp(now, UTC).astimezone(ET)
    today = et.date()

    if now - meta.get("series_at", 0) > SERIES_REFRESH or not meta.get("series"):
        try:
            found = discover(http)
            if found:
                meta["series"], meta["series_at"] = found, now
        except Exception as e:
            notes.append(f"series discovery failed: {str(e)[:160]}")
    series = meta.get("series") or {}

    key = today.isoformat()
    if key not in meta["scanned"] and series:
        if WINDOW[0] <= et.time() < WINDOW[1]:
            k = Kalshi(http)
            for ticker, city in sorted(series.items()):
                for c in candidates(k.series(ticker), today):
                    m = c["market"]
                    if m["ticker"] in trades:
                        continue
                    fee = order_fee(c["no_ask"])
                    trades[m["ticker"]] = {
                        "ticker": m["ticker"], "series": ticker, "city": city, "day": key,
                        "bracket": str(m.get("yes_sub_title") or m.get("subtitle") or ""),
                        "opened": et.isoformat(timespec="seconds"), "yes_bid": c["yes_bid"], "yes_ask": c["yes_ask"],
                        "no_ask": c["no_ask"], "fee": fee, "cost": round(CONTRACTS * c["no_ask"] + fee, 2),
                        "volume": m.get("volume_fp") or m.get("volume") or "", "status": "open", "result": "",
                        "settled": "", "pnl": "", "pnl_c": ""}
                    opened.append(trades[m["ticker"]])
            meta["scanned"][key] = {"at": et.isoformat(timespec="seconds"), "trades": len(opened)}
        elif et.time() >= WINDOW[1]:
            meta["scanned"][key] = {"at": et.isoformat(timespec="seconds"), "missed": True, "trades": 0}
            notes.append(f"missed the {WINDOW[0]:%H:%M}-{WINDOW[1]:%H:%M} ET window on {key}")

    settled = 0
    k = Kalshi(http)
    for t in trades.values():
        if t["status"] != "open" or dt.date.fromisoformat(t["day"]) >= today:
            continue
        try:
            result = (k.market(t["ticker"]) or {}).get("result")
        except Exception as e:
            notes.append(f"{t['ticker']}: {str(e)[:120]}")
            continue
        if result not in ("yes", "no"):
            continue
        payout = CONTRACTS * (1.0 if result == "no" else 0.0)
        pnl = round(payout - float(t["cost"]), 2)
        t.update(status="settled", result=result, settled=dt.datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds"),
                 pnl=pnl, pnl_c=round(pnl / CONTRACTS * 100, 3))
        settled += 1

    save(state, trades, meta)
    (state / "summary.md").write_text(report(trades, meta, now))
    return {"opened": opened, "settled": settled, "notes": notes}


def clustered(trades: list[dict]) -> dict | None:
    """Mean P&L per contract in cents, with a 95% interval clustered by city-day."""
    if not trades:
        return None
    n = len(trades)
    mean = sum(float(t["pnl_c"]) for t in trades) / n
    groups: dict[tuple, list[float]] = {}
    for t in trades:
        groups.setdefault((t["city"], t["day"]), []).append(float(t["pnl_c"]))
    g = len(groups)
    if g < 2:
        return {"n": n, "mean": mean, "lo": None, "hi": None, "groups": g}
    var = sum((sum(v) - len(v) * mean) ** 2 for v in groups.values()) / n ** 2 * g / (g - 1)
    half = 1.96 * math.sqrt(var)
    return {"n": n, "mean": mean, "lo": mean - half, "hi": mean + half, "groups": g}


def verdict(s: dict | None, today: dt.date) -> str:
    n = s["n"] if s else 0
    if n < BAR_TRADES and today < BAR_DATE:
        return f"Running: {n} of {BAR_TRADES} settled trades (or judged on {BAR_DATE}). Not judged yet."
    if s and s["lo"] is not None and s["lo"] > 0:
        return f"**Passed**: over {n} trades the 95% interval ({s['lo']:+.2f}c to {s['hi']:+.2f}c) lies above zero."
    band = f"({s['lo']:+.2f}c to {s['hi']:+.2f}c)" if s and s["lo"] is not None else "(too few to say)"
    return f"**Failed**: over {n} trades the 95% interval {band} does not lie above zero."


def report(trades: dict[str, dict], meta: dict, now: int) -> str:
    today = dt.datetime.fromtimestamp(now, UTC).astimezone(ET).date()
    done = [t for t in trades.values() if t["status"] == "settled"]
    s = clustered(done)
    losses = [t for t in done if t["result"] == "yes"]
    lines = ["# Storm Chaser: weather long shots (paper)", "",
             f"Started {meta.get('started')}. Rule: at 17:00 ET, buy {CONTRACTS} NO on every daily-high bracket with a "
             f"YES bid of 1-4c (spread 5c or less), at the ask plus the taker fee, and hold. "
             f"{len(meta.get('series') or {})} cities watched.", "",
             f"**Verdict:** {verdict(s, today)}", ""]
    if s:
        band = f"{s['lo']:+.2f}c to {s['hi']:+.2f}c" if s["lo"] is not None else "–"
        lines += ["| Settled | Losses | Mean per contract | 95% interval (city-days) | P&L | Open |", "|---|---|---|---|---|---|",
                  f"| {s['n']} | {len(losses)} | {s['mean']:+.2f}c | {band} | ${sum(float(t['pnl']) for t in done):+,.2f} "
                  f"| {sum(t['status'] == 'open' for t in trades.values())} |", ""]
    else:
        lines += [f"No settled trades yet; {sum(t['status'] == 'open' for t in trades.values())} open.", ""]
    lines += [f"Backtest it is testing: {BACKTEST['mean_c']:+.2f}c per contract (90% CI {BACKTEST['lo_c']:+.2f}c to "
              f"{BACKTEST['hi_c']:+.2f}c) over {BACKTEST['trades']} trades with {BACKTEST['losses']} losses. "
              "One loss costs about 50 wins, so the loss rate decides it.", ""]
    by_city: dict[str, list[dict]] = {}
    for t in done:
        by_city.setdefault(t["city"], []).append(t)
    if by_city:
        rows = []
        for city, ts in sorted(by_city.items()):
            cs = clustered(ts)
            rows.append(f"| {city} | {len(ts)} | {sum(t['result'] == 'yes' for t in ts)} | {cs['mean']:+.2f}c |")
        lines += ["## By city", "", "| City | Settled | Losses | Mean per contract |", "|---|---|---|---|", *rows, ""]
    if losses:
        lines += ["## Losses", "", "| Day | City | Bracket | Paid |", "|---|---|---|---|",
                  *[f"| {t['day']} | {t['city']} | {t['bracket']} | {float(t['no_ask']) * 100:.0f}c |" for t in losses], ""]
    days = sorted(meta.get("scanned", {}).items())[-10:]
    if days:
        lines += ["## Recent scans", "", "| Day | Scanned at (ET) | Trades |", "|---|---|---|",
                  *[f"| {d} | {v['at'][11:16]}{' (missed the window)' if v.get('missed') else ''} | {v['trades']} |"
                    for d, v in days], ""]
    return "\n".join(lines) + "\n"


def main() -> None:
    state = pathlib.Path(os.environ.get("STATE_DIR", "out")) / "weather"
    http = poly.Http()
    res = tick(state, http, int(time.time()))
    print(f"weather: {len(res['opened'])} opened, {res['settled']} settled, {http.count} requests")
    for n in res["notes"]:
        print("note:", n)


if __name__ == "__main__":
    main()
