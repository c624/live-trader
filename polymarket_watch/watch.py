"""Paper-copy the watched Polymarket wallets forward, one tick at a time.

Each tick reads every watched wallet's recent fills, rebuilds its positions
with the backtest's own rules, and paper-copies each new opening buy at the
live CLOB ask plus the taker fee (walked for the stake, skipped when the price
has already run past the wallet's fill). Copies mirror the wallet's later
sells at the live bid, and settle on the market's resolution. Sports copies
also record the same bet on Kalshi when one is listed, priced at Kalshi's ask,
since that is the venue a US follower can actually use.

Nothing here signs, sends or places an order. New sports copies are announced
on Telegram (the repo's existing bot) when its secrets are present.

Env: STATE_DIR (wallet-watch state checkout), FIRST_LOOKBACK_MIN (60): on the
first tick, positions opened this recently are copied; older ones are history.
Writes STATE_DIR/{watch.json,fills.csv.gz,copies.csv,summary.md,alerts.jsonl}.
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import json
import math
import os
import pathlib
import time
from collections import Counter

from polymarket_watch import kalshi, poly

ROOT = pathlib.Path(__file__).resolve().parent
STAKE = 100.0          # paper dollars per copy, the backtest's stake, so ROI compares directly
CHASE = 0.10           # skip when the midpoint has run this far past the wallet's fill
MIN_NOTIONAL = 5.0     # opening buys under $5 are tests or reward farming, not signals
SEED_DAYS = 30         # fills read on the first tick, so adds are not mistaken for openings
OVERLAP = 3 * 3600     # re-read this much before the last fill seen; the Data API indexes late
KEEP_DAYS = 35         # fills older than this are dropped unless an open copy still needs them
BAR_COPIES = 300       # settled sports copies at which the forward test is judged
BAR_DATE = dt.datetime(2026, 11, 6, tzinfo=dt.timezone.utc)
FILL_FIELDS = ["wallet", "ts", "token", "cond", "side", "price", "size", "tx", "title", "outcome", "slug",
               "event_slug"]
COPY_FIELDS = ["id", "wallet", "name", "opened", "detected", "delay_min", "title", "outcome", "category",
               "market_type", "game_start", "wallet_fill", "wallet_cost", "status", "mid", "ask", "avg_price",
               "fee_rate", "fees", "shares", "spent", "sold_shares", "sale_cash", "sells_mirrored", "mirrored_through", "exit",
               "settled", "value", "pnl", "roi", "close_mid", "close_at", "clv_c", "wallet_clv_c", "kalshi_ticker", "kalshi_side", "kalshi_label", "kalshi_ask",
               "kalshi_note", "kalshi_result", "kalshi_roi", "token", "cond", "event_slug", "slug"]


def iso(t) -> str:
    return dt.datetime.fromtimestamp(int(t), dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else ""


def from_iso(s: str) -> int | None:
    return int(poly.parse_time(s).timestamp()) if s else None


def r4(x):
    return "" if x is None else round(x, 4)


def fnum(v) -> float:
    return poly.num(v) or 0.0


def load_wallets(path: pathlib.Path = ROOT / "wallets.json") -> list[dict]:
    return json.loads(path.read_text())["wallets"]


def load_fills(state: pathlib.Path) -> dict[str, list[dict]]:
    out = {}
    path = state / "fills.csv.gz"
    if path.exists():
        with gzip.open(path, "rt", newline="") as f:
            for r in csv.DictReader(f):
                out.setdefault(r["wallet"], []).append(
                    {**r, "ts": int(r["ts"]), "price": float(r["price"]), "size": float(r["size"])})
    return out


def save_fills(state: pathlib.Path, log: dict[str, list[dict]]) -> None:
    with gzip.open(state / "fills.csv.gz", "wt", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FILL_FIELDS, extrasaction="ignore")
        w.writeheader()
        for wallet, fs in sorted(log.items()):
            w.writerows({**x, "wallet": wallet} for x in sorted(fs, key=lambda x: x["ts"]))


def load_copies(state: pathlib.Path) -> dict[str, dict]:
    path = state / "copies.csv"
    if not path.exists():
        return {}
    with open(path, newline="") as f:
        return {r["id"]: r for r in csv.DictReader(f)}


def save_copies(state: pathlib.Path, copies: dict[str, dict]) -> None:
    with open(state / "copies.csv", "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COPY_FIELDS, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(copies.values(), key=lambda c: (c["detected"], c["id"])))


def merge(old: list[dict], new: list[dict]) -> list[dict]:
    seen = {poly.fill_key(f) for f in old}
    return old + [f for f in new if poly.fill_key(f) not in seen and not seen.add(poly.fill_key(f))]


def prune(fs: list[dict], now: int, keep_tokens: set[str]) -> list[dict]:
    """Drop a token's fills only once all of them are old, so a long-held position is never cut in two
    (its later adds would read as a new opening)."""
    cut = now - KEEP_DAYS * 86400
    recent = {f["token"] for f in fs if f["ts"] >= cut} | keep_tokens
    return [f for f in fs if f["token"] in recent]


def entry(p: dict, m: dict | None, book: dict | None, now: int) -> dict:
    """The paper copy of one opening buy, or the reason there is none."""
    row = {"wallet_fill": r4(p["fill"]), "wallet_cost": r4(p["cost"])}
    if p["cost"] < MIN_NOTIONAL:
        return row | {"status": "too_small"}
    if m is None:
        return row | {"status": "no_market"}
    if m["closed"] or not m["accepting"]:
        return row | {"status": "closed_before_entry"}
    if p["close_ts"] and p["close_ts"] <= now:
        return row | {"status": "exited_before_entry"}
    if not book or not book["asks"]:
        return row | {"status": "no_book"}
    best_ask = book["asks"][0][0]
    best_bid = book["bids"][0][0] if book["bids"] else None
    mid = (best_ask + best_bid) / 2 if best_bid is not None else best_ask
    row |= {"mid": r4(mid), "ask": r4(best_ask), "fee_rate": m["fee_rate"]}
    if mid - p["fill"] > CHASE:
        return row | {"status": "skipped_chase"}
    if best_ask >= 0.99:
        return row | {"status": "priced_out"}
    fill = poly.walk_buy(book["asks"], STAKE, m["fee_rate"], m["fee_exp"])
    if not fill["shares"]:
        return row | {"status": "no_book"}
    return row | {"status": "copied", "avg_price": r4(fill["avg"]), "fees": r4(fill["fees"]),
                  "shares": r4(fill["shares"]), "spent": r4(fill["spent"]), "sold_shares": 0, "sale_cash": 0,
                  "sells_mirrored": 0, "exit": "open"}


def unmirrored(c: dict, p: dict) -> list[tuple[int, float]]:
    """The wallet's sells after the copy was made and after the last one already mirrored. A sell
    indexed late, behind one already mirrored, is missed rather than counted twice."""
    since = max(from_iso(c["detected"]), from_iso(c.get("mirrored_through") or "") or 0)
    return [(ts, frac) for ts, frac in p["sells"] if ts > since]


def mirror_sells(c: dict, p: dict, m: dict, book: dict | None, now: int) -> bool:
    """Sell the same fraction the wallet sold, at the live bid. True if it sold."""
    todo = unmirrored(c, p)
    if not todo or not book or not book["bids"]:
        return False
    left = fnum(c["shares"]) - fnum(c["sold_shares"])
    cash = fnum(c["sale_cash"])
    for _, frac in todo:
        sale = poly.walk_sell(book["bids"], left * frac, m["fee_rate"], m["fee_exp"])
        cash += sale["cash"]
        left -= sale["sold"]
    c |= {"sold_shares": r4(fnum(c["shares"]) - left), "sale_cash": r4(cash),
          "sells_mirrored": int(fnum(c["sells_mirrored"])) + len(todo), "mirrored_through": iso(todo[-1][0]),
          "exit": "sold" if left <= 1e-6 else "part sold"}
    if left <= 1e-6:
        finish(c, 0.0, "sold", now)
    return True


def finish(c: dict, value: float, how: str, now: float) -> None:
    left = fnum(c["shares"]) - fnum(c["sold_shares"])
    payout = fnum(c["sale_cash"]) + left * value
    spent = fnum(c["spent"])
    exit_ = how if c["exit"] in ("open", how) else f"{c['exit']}+{how}"
    c |= {"settled": iso(now), "value": r4(value), "pnl": r4(payout - spent), "roi": r4((payout - spent) / spent),
          "exit": exit_}


def closing_line(c: dict, m: dict, now: int) -> None:
    """Keep the market's price for this outcome up to the start: the last one before the game is the
    closing line. A copy that beats it was ahead of the market, which shows skill in far fewer bets than
    wins and losses do. Copies made after the start have no closing line."""
    start = from_iso(c.get("game_start") or "")
    if m["closed"] or (start and now >= start) or (start and from_iso(c["detected"]) >= start):
        return
    mid = m["price"].get(c["token"])
    if mid is None or not 0 < mid < 1:
        return
    c |= {"close_mid": r4(mid), "close_at": iso(now), "clv_c": r4((mid - fnum(c["avg_price"])) * 100),
          "wallet_clv_c": r4((mid - fnum(c["wallet_fill"])) * 100)}


def kalshi_roi(ask: float, result: str) -> float:
    cost = ask + kalshi.taker_fee(ask)
    return ((1.0 if result == "win" else 0.0) - cost) / cost


def tick(state: pathlib.Path, http: poly.Http, now: int, wallets: list[dict], k: kalshi.Kalshi | None = None,
         first_lookback: int = 3600) -> dict:
    """One pass. Returns {"alerts": [...], "notes": [...]} and writes every state file."""
    state.mkdir(parents=True, exist_ok=True)
    meta_path = state / "watch.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {
        "started": iso(now - first_lookback), "started_ts": now - first_lookback, "ticks": 0, "wallets": {}}
    log, copies = load_fills(state), load_copies(state)
    activity, notes = poly.Activity(http), []
    k = k or kalshi.Kalshi(http)

    for w in wallets:
        addr, wm = w["wallet"], meta["wallets"].setdefault(w["wallet"], {})
        start = (wm["last_ts"] - OVERLAP) if wm.get("last_ts") else now - SEED_DAYS * 86400
        try:
            new = activity.fills(addr, start, now)
        except Exception as e:
            wm["error"] = f"{iso(now)} {str(e)[:200]}"
            notes.append(f"{w['name'] or addr[:10]}: {str(e)[:120]}")
            continue
        log[addr] = merge(log.get(addr, []), new)
        wm |= {"last_ts": max([f["ts"] for f in log[addr]] + [wm.get("last_ts") or 0]) or now, "error": ""}

    names = {w["wallet"]: w["name"] or f"{w['wallet'][:6]}..{w['wallet'][-4:]}" for w in wallets}
    by_token = {}
    for addr, fs in log.items():
        if addr in names:
            for p in poly.build_positions(fs):
                by_token.setdefault((addr, p["token"]), []).append(p)
    copied_at = {}
    for c in copies.values():
        if c["status"] != "no_market":  # a failed market lookup is tried again next tick
            copied_at.setdefault((c["wallet"], c["token"]), []).append(from_iso(c["opened"]))
    fresh = []
    for (addr, token), ps in by_token.items():
        for p in ps:
            mine = [t for t in copied_at.get((addr, token), []) if p["open_ts"] <= t <= (p["close_ts"] or now)]
            if p["open_ts"] >= meta["started_ts"] and not mine:
                fresh.append((f"{addr}:{token}:{p['open_ts']}", addr, p))
    live = [c for c in copies.values() if c["status"] == "copied" and not c["settled"]]
    markets = poly.markets_for(http, sorted({p["cond"] for _, _, p in fresh} | {c["cond"] for c in live}))

    alerts = []
    for pid, addr, p in sorted(fresh, key=lambda x: x[2]["open_ts"]):
        m = markets.get(p["cond"])
        needs_book = m and p["cost"] >= MIN_NOTIONAL and not m["closed"] and not (p["close_ts"] and p["close_ts"] <= now)
        row = entry(p, m, poly.book(http, p["token"]) if needs_book else None, now)
        row |= {"id": pid, "wallet": addr, "name": names[addr], "opened": iso(p["open_ts"]), "detected": iso(now),
                "delay_min": round((now - p["open_ts"]) / 60, 1), "title": p["title"], "outcome": p["outcome"],
                "category": (m or {}).get("category", "unknown"), "market_type": (m or {}).get("type", ""),
                "game_start": iso((m or {}).get("game_start")), "token": p["token"], "cond": p["cond"],
                "event_slug": p.get("event_slug", ""), "slug": p.get("slug", ""), "settled": ""}
        if row["status"] == "copied" and row["category"] == "sports":
            try:
                km = kalshi.match(k, m, p["outcome"], p.get("event_slug", ""), p.get("slug", ""))
            except Exception as e:  # a matcher bug must not cost the copy
                km = {"note": f"matcher error: {str(e)[:80]}"}
            row |= {"kalshi_ticker": km.get("ticker", ""), "kalshi_side": km.get("side", ""),
                    "kalshi_label": km.get("label", ""), "kalshi_ask": r4(km.get("ask")),
                    "kalshi_note": km.get("note", "")}
            alerts.append(row | {"kalshi_payout": km.get("payout")})
        copies[pid] = row

    for c in copies.values():
        if c["status"] != "copied" or c["settled"]:
            continue
        m = markets.get(c["cond"])
        if not m:
            continue
        closing_line(c, m, now)
        opened = from_iso(c["opened"])
        pos = next((p for p in by_token.get((c["wallet"], c["token"]), [])
                    if p["open_ts"] <= opened <= (p["close_ts"] or now)), None)
        if pos and not m["closed"] and unmirrored(c, pos):
            mirror_sells(c, pos, m, poly.book(http, c["token"]), now)
        if not c["settled"] and m["resolved"]:
            finish(c, m["price"].get(c["token"]) or 0.0, "resolved", now)
    for c in copies.values():
        if c.get("kalshi_ticker") and c.get("kalshi_ask") not in ("", None) and not c.get("kalshi_result") and c["settled"]:
            result = kalshi.settle(k, c["kalshi_ticker"], c["kalshi_side"])
            if result:
                c |= {"kalshi_result": result, "kalshi_roi": r4(kalshi_roi(fnum(c["kalshi_ask"]), result))}

    open_tokens = {c["token"] for c in copies.values() if c["status"] == "copied" and not c["settled"]}
    for addr in log:
        log[addr] = prune(log[addr], now, open_tokens)
    meta |= {"ticks": meta["ticks"] + 1, "last_tick": iso(now), "requests_last_tick": http.count,
             "activity_api": activity.used}
    save_fills(state, log)
    save_copies(state, copies)
    meta_path.write_text(json.dumps(meta, indent=1))
    (state / "summary.md").write_text(report(copies, meta, wallets, now))
    if alerts:
        with open(state / "alerts.jsonl", "a") as f:
            for a in alerts:
                f.write(json.dumps({k2: a.get(k2) for k2 in ("id", "name", "opened", "detected", "title", "outcome",
                                                             "ask", "wallet_fill", "kalshi_ticker", "kalshi_side",
                                                             "kalshi_ask")}) + "\n")
    return {"alerts": alerts, "notes": notes, "fresh": len(fresh)}


def pooled(rois: list[float]) -> dict:
    n = len(rois)
    if not n:
        return {"n": 0, "roi": None, "lo": None, "hi": None}
    mean = sum(rois) / n
    se = math.sqrt(sum((x - mean) ** 2 for x in rois) / max(n - 1, 1)) / math.sqrt(n)
    return {"n": n, "roi": mean, "lo": mean - 2 * se, "hi": mean + 2 * se}


def pct(x) -> str:
    return "n/a" if x in (None, "") else f"{float(x) * 100:+.1f}%"


def record(rows: list[dict]) -> str:
    wins = sum(fnum(r["pnl"]) > 0 for r in rows)
    return f"{wins}-{len(rows) - wins}"


def verdict(s: dict, n_open: int, now: int) -> str:
    due = s["n"] >= BAR_COPIES or now >= BAR_DATE.timestamp()
    if not due:
        return (f"Running: {s['n']} of {BAR_COPIES} settled sports copies (or {BAR_DATE:%Y-%m-%d}), {n_open} open. "
                "No verdict before then.")
    if s["n"] and s["lo"] > 0:
        return f"**Passed**: the 95% interval ({pct(s['lo'])} to {pct(s['hi'])}) lies above zero over {s['n']} copies."
    return f"**Failed**: over {s['n']} copies the 95% interval ({pct(s['lo'])} to {pct(s['hi'])}) does not lie above zero."


def report(copies: dict[str, dict], meta: dict, wallets: list[dict], now: int) -> str:
    rows = list(copies.values())
    made = [r for r in rows if r["status"] == "copied"]
    sports = [r for r in made if r["category"] == "sports"]
    done = [r for r in sports if r["settled"]]
    s = pooled([fnum(r["roi"]) for r in done])
    other = [r for r in made if r["category"] != "sports" and r["settled"]]
    o = pooled([fnum(r["roi"]) for r in other])
    ks = [r for r in done if r.get("kalshi_result")]
    kp = pooled([fnum(r["kalshi_roi"]) for r in ks])
    poly_on_k = pooled([fnum(r["roi"]) for r in ks])
    lines = [
        "# Polymarket wallet watch (paper)", "",
        f"Started {meta['started']}; last tick {meta.get('last_tick', '')}, tick {meta['ticks']}. "
        f"Watching {len(wallets)} wallets fixed on 2026-10-06. ${STAKE:.0f} paper per copy at the live ask plus the "
        f"taker fee; chases over {CHASE * 100:.0f}c past the wallet's fill are skipped.", "",
        f"**Verdict:** {verdict(s, len(sports) - len(done), now)}", "",
        "| Arm | Settled | W-L | Mean ROI | 95% interval | P&L | Open |",
        "|---|---|---|---|---|---|---|",
        f"| Sports (judged) | {s['n']} | {record(done)} | {pct(s['roi'])} | {pct(s['lo'])} to {pct(s['hi'])} | "
        f"${sum(fnum(r['pnl']) for r in done):+,.0f} | {len(sports) - len(done)} |",
        f"| Not sports (recorded, not judged) | {o['n']} | {record(other)} | {pct(o['roi'])} | "
        f"{pct(o['lo'])} to {pct(o['hi'])} | ${sum(fnum(r['pnl']) for r in other):+,.0f} | "
        f"{sum(1 for r in made if r['category'] != 'sports' and not r['settled'])} |",
        f"| Same bets on Kalshi | {kp['n']} | {sum(r['kalshi_result'] == 'win' for r in ks)}-"
        f"{sum(r['kalshi_result'] == 'loss' for r in ks)} | {pct(kp['roi'])} | {pct(kp['lo'])} to {pct(kp['hi'])} | "
        f"(Polymarket on the same {poly_on_k['n']}: {pct(poly_on_k['roi'])}) | |", "",
    ]
    final = [r for r in sports if r.get("clv_c") not in ("", None) and (r["settled"] or (
        r.get("game_start") and from_iso(r["game_start"]) <= now))]
    if final:
        clv = pooled([fnum(r["clv_c"]) for r in final])
        own = pooled([fnum(r["wallet_clv_c"]) for r in final])
        toward = sum(fnum(r["clv_c"]) > 0.5 for r in final) / len(final)
        lines += [f"**Closing line** (price at the last tick before the start, against the entry; {len(final)} sports "
                  f"copies): ours {clv['roi']:+.1f}c (band {clv['lo']:+.1f}c to {clv['hi']:+.1f}c), the wallets' own fills "
                  f"{own['roi']:+.1f}c; the price moved our way after {toward:.0%} of copies. Beating the close is the "
                  "early sign of an edge; it is recorded, not judged.", ""]
    funnel = Counter(r["status"] for r in rows)
    matched = sum(1 for r in sports if r.get("kalshi_ticker"))
    lines += [f"Opening buys seen since the start: {len(rows)} ({', '.join(f'{k} {v}' for k, v in funnel.most_common())}). "
              f"Sports copies with a Kalshi match: {matched} of {len(sports)}.", "",
              "## By wallet", "", "| Wallet | Copies | Settled | W-L | Mean ROI | Backtest ROI (60 min) |",
              "|---|---|---|---|---|---|"]
    for w in wallets:
        mine = [r for r in made if r["wallet"] == w["wallet"]]
        if not mine:
            continue
        settled = [r for r in mine if r["settled"]]
        lines.append(f"| {w['name'] or w['wallet'][:10]} | {len(mine)} | {len(settled)} | {record(settled)} | "
                     f"{pct(pooled([fnum(r['roi']) for r in settled])['roi'])} | {pct(w.get('backtest_roi_60m'))} |")
    lines += ["", "## Latest copies", "", "| Detected | Wallet | Bet | Ask | Kalshi | Result |", "|---|---|---|---|---|---|"]
    for r in sorted(made, key=lambda r: r["detected"], reverse=True)[:25]:
        kal = (f"{r['kalshi_side'].upper()} {r['kalshi_label']} {fnum(r['kalshi_ask']) * 100:.0f}c"
               if r.get("kalshi_ticker") else r.get("kalshi_note") or "")
        res = f"{pct(r['roi'])} ({r['exit']})" if r["settled"] else r["exit"]
        lines.append(f"| {r['detected'][5:16]} | {r['name']} | {r['title']}: {r['outcome']} | "
                     f"{fnum(r['ask']) * 100:.0f}c | {kal} | {res} |")
    return "\n".join(lines) + "\n"


def alert_text(alerts: list[dict], copies: dict[str, dict]) -> list[str]:
    """Telegram messages, at most ~3,500 characters each. Bets with a Kalshi twin get the detail a
    follower needs; the rest are one line each, since they can only be followed on Polymarket."""
    done = [c for c in copies.values() if c["status"] == "copied" and c["category"] == "sports" and c["settled"]]
    tally = (f"Paper record so far: {record(done)}, {pct(pooled([fnum(c['roi']) for c in done])['roi'])} per copy "
             f"over {len(done)} settled.")
    blocks, others = [], []
    for a in alerts:
        start = poly.parse_time(a["game_start"]) if a.get("game_start") else None
        when = ""
        if start:
            mins = (start.timestamp() - from_iso(a["detected"])) / 60
            when = f", starts in {mins / 60:.1f}h" if mins > 0 else ", game already started"
        bought = (f"{a['name']} bought {a['outcome']} at {fnum(a['wallet_fill']) * 100:.0f}c "
                  f"(${fnum(a['wallet_cost']):,.0f}) {a['delay_min']:.0f} min ago")
        if not a.get("kalshi_ticker"):
            others.append(f"- {a['title']}: {a['outcome']} ({a['name']}, Polymarket {fnum(a['ask']) * 100:.0f}c)")
            continue
        pays = f" (pays {a['kalshi_payout']}x)" if a.get("kalshi_payout") else ""
        blocks.append("\n".join([
            bought, f"{a['title']}{when}", f"Polymarket ask now {fnum(a['ask']) * 100:.0f}c",
            f"Kalshi: {a['kalshi_side'].upper()} on '{a['kalshi_label']}' at {fnum(a['kalshi_ask']) * 100:.0f}c{pays} "
            f"{a['kalshi_ticker']}"]))
    if others:
        blocks.append("Copied on paper, no Kalshi twin:\n" + "\n".join(others))
    head = "Polymarket wallet watch: PAPER test, not a proven edge yet."
    out, cur = [], head
    for b in blocks + [tally]:
        if len(cur) + len(b) + 2 > 3500:
            out.append(cur)
            cur = b
        else:
            cur += "\n\n" + b
    out.append(cur)
    return out


def main() -> None:
    state = pathlib.Path(os.environ.get("STATE_DIR", "out"))
    http = poly.Http()
    now = int(time.time())
    res = tick(state, http, now, load_wallets(), first_lookback=int(os.environ.get("FIRST_LOOKBACK_MIN", "60")) * 60)
    print(f"{res['fresh']} new opening buys, {len(res['alerts'])} sports copies to announce, {http.count} requests")
    for n in res["notes"]:
        print("note:", n)
    if res["alerts"]:
        from live_trader.src import notify
        for text in alert_text(res["alerts"], load_copies(state)):
            print(text)
            if not notify.send(text):
                print("telegram: not sent (no secrets, or the send failed)")
    print((state / "summary.md").read_text())


if __name__ == "__main__":
    main()
