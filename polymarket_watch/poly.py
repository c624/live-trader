"""Polymarket reads for the wallet watch: the Data API for a wallet's fills,
Gamma for what a market is and whether it has resolved, and the CLOB for the
book a follower would actually trade against. Read-only; nothing here signs or
places an order.

The fill parsing and position building are the backtest's own (tradingbotgpt,
prediction_markets/wallets.py, 2026-10-06), copied rather than re-derived, so
the forward test copies exactly what the backtest copied.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import time
import unicodedata
from collections import defaultdict

import httpx

DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
MAX_FILLS = 3000          # per wallet per read; the watched wallets trade far less than this
GAMMA_BATCH = 40
SPORTS_FEE = 0.05         # taker fee rate for a sports market that lists none
CATEGORY_FEES = {"sports": 0.05, "economics": 0.05, "culture": 0.05, "weather": 0.05, "crypto": 0.07,
                 "politics": 0.04, "finance": 0.04, "tech": 0.04, "mentions": 0.04}
CATEGORY_TAGS = [
    ("sports", {"sports", "nfl", "nba", "mlb", "nhl", "wnba", "soccer", "tennis", "golf", "ufc", "mma", "boxing",
                "cricket", "f1", "ncaa", "cfb", "college-football", "college-basketball"}),
    ("esports", {"esports"}),
    ("crypto", {"crypto", "bitcoin", "ethereum", "solana", "xrp", "crypto-prices"}),
    ("geopolitics", {"geopolitics", "world", "middle-east", "ukraine", "israel"}),
    ("politics", {"politics", "elections", "us-politics", "us-election", "trump"}),
    ("economics", {"economy", "economics", "fed", "inflation", "fed-rates"}),
    ("finance", {"finance", "stocks", "business", "earnings", "ipos"}),
    ("tech", {"tech", "ai", "science"}),
    ("weather", {"weather", "climate"}),
    ("mentions", {"mentions"}),
    ("culture", {"pop-culture", "culture", "celebrities", "movies", "music", "awards", "entertainment"}),
]


def num(v) -> float | None:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def first(row: dict, *keys):
    """The first non-empty value among `keys` (v1 answers in camelCase, v2 in snake_case)."""
    return next((row[k] for k in keys if row.get(k) not in (None, "")), None)


def parse_time(s: str) -> dt.datetime:
    """Polymarket mixes "2026-10-01 02:00:00+00" and "2026-10-01T02:00:00Z"."""
    s = s.strip().replace(" ", "T").replace("Z", "+00:00")
    if s.endswith("+00"):
        s += ":00"
    t = dt.datetime.fromisoformat(s)
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def epoch(v) -> int | None:
    """Unix seconds from seconds, milliseconds or an ISO time."""
    x = num(v)
    if x is None:
        try:
            return int(parse_time(str(v)).timestamp()) if v else None
        except ValueError:
            return None
    return int(x / 1000) if x > 1e11 else int(x)


def jlist(v) -> list:
    """Gamma sends some lists as JSON strings."""
    if isinstance(v, str):
        try:
            v = json.loads(v)
        except ValueError:
            return []
    return v if isinstance(v, list) else []


def fold(s: str) -> str:
    """Lowercase ASCII: "Alavés" and "Alaves" are the same team."""
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


class Http:
    """JSON GETs at least `min_interval` apart, retried on 429/5xx and dropped connections."""

    def __init__(self, client: httpx.Client | None = None, min_interval: float = 0.1, sleep=time.sleep):
        self.client = client or httpx.Client(timeout=30, follow_redirects=True)
        self.min_interval, self.sleep = min_interval, sleep
        self.count, self._last = 0, 0.0

    def get(self, url: str, params: dict | None = None):
        problem = ""
        for attempt in range(5):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                self.sleep(wait)
            self._last = time.monotonic()
            self.count += 1
            try:
                r = self.client.get(url, params=params)
            except httpx.TransportError as e:
                problem, pause = repr(e), 2 ** attempt
            else:
                if r.status_code != 429 and r.status_code < 500:
                    r.raise_for_status()
                    return r.json()
                problem, pause = f"HTTP {r.status_code}", num(r.headers.get("retry-after")) or 2 ** attempt
            self.sleep(min(pause, 30))
        raise RuntimeError(f"{url}: {problem} after 5 tries")


def pages(http: Http, path: str, params: dict, limit: int, cap: int) -> list:
    """Follow a Data API v2 cursor until it runs out or `cap` rows are in."""
    rows, cursor = [], None
    while len(rows) < cap:
        body = http.get(f"{DATA}{path}", {**params, "limit": limit, **({"cursor": cursor} if cursor else {})})
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise ValueError(f"unexpected v2 shape: {str(body)[:120]}")
        rows += body["data"]
        cursor = (body.get("pagination") or {}).get("next_cursor")
        if not cursor or cursor == "LTE=" or not body["data"]:  # "LTE=" is the SDK's end sentinel
            break
    return rows


def offsets(http: Http, path: str, params: dict, limit: int, cap: int) -> list:
    """Page a Data API v1 list by offset."""
    rows = []
    for offset in range(0, cap, limit):
        size = min(limit, cap - offset)
        page = http.get(f"{DATA}{path}", {**params, "limit": size, "offset": offset})
        if not isinstance(page, list):
            raise ValueError(f"unexpected v1 shape: {str(page)[:120]}")
        rows += page
        if len(page) < size:
            break
    return rows


def norm_fill(r: dict) -> dict | None:
    """One trade fill in a common shape, or None for combos and anything malformed."""
    if not isinstance(r, dict) or str(r.get("type") or "TRADE").upper() != "TRADE" or r.get("is_combo"):
        return None
    token, cond = first(r, "token_id", "asset", "asset_id"), str(first(r, "condition_id", "conditionId") or "").lower()
    side, ts = str(r.get("side") or "").upper(), epoch(r.get("timestamp"))
    price, size = num(r.get("price")), num(r.get("size"))
    if not token or not cond.startswith("0x") or side not in ("BUY", "SELL") or ts is None or not size or size < 0:
        return None
    if price is None or not 0 < price < 1:
        return None
    return {"ts": ts, "token": str(token), "cond": cond, "side": side, "price": price, "size": size,
            "tx": str(first(r, "transaction_hash", "transactionHash") or ""), "title": str(r.get("title") or ""),
            "outcome": str(r.get("outcome") or ""), "slug": str(r.get("slug") or ""),
            "event_slug": str(first(r, "event_slug", "eventSlug") or "")}


def fill_key(f: dict) -> tuple:
    return (f["tx"], f["token"], f["side"], round(f["size"], 6), round(f["price"], 6), f["ts"])


class Activity:
    """A wallet's fills from the Data API. v1 answered the backtest's activity reads; it retires on
    2026-10-24, after which v2 must. Until a version has returned rows, an empty answer may be a
    wrong route rather than a quiet wallet, so the other is asked too and the longer answer kept."""

    def __init__(self, http: Http):
        self.http, self.order, self.used, self.proven = http, ["v1", "v2"], "", set()

    def fills(self, wallet: str, start: int, end: int) -> list[dict]:
        calls = {
            "v1": lambda: offsets(self.http, "/activity", {"user": wallet, "type": "TRADE", "start": start, "end": end,
                                                           "sortBy": "TIMESTAMP", "sortDirection": "DESC"}, 500,
                                  MAX_FILLS),
            "v2": lambda: pages(self.http, "/v2/activity", {"user": wallet, "type": "TRADE", "start": start,
                                                            "end": end, "sort_direction": "DESC"}, 1000, MAX_FILLS),
        }
        errors, answers = [], []
        for v in list(self.order):
            try:
                raw = calls[v]()
                if raw and not any(map(norm_fill, raw)):
                    raise ValueError(f"{len(raw)} rows, none usable, e.g. {str(raw[0])[:150]}")
            except Exception as e:
                errors.append(f"{v}: {str(e)[:200]}")
                continue
            answers.append((v, raw))
            if raw:
                self.proven.add(v)
            if raw or v in self.proven:
                break
        if not answers:
            raise RuntimeError("; ".join(errors))
        v, raw = max(answers, key=lambda a: len(a[1]))
        self.order.remove(v)
        self.order.insert(0, v)
        self.used = v
        out, seen = [], set()
        for f in map(norm_fill, raw):
            # Offset pages shift when new fills land mid-read, repeating rows at the seams.
            if f and start <= f["ts"] <= end and fill_key(f) not in seen:
                seen.add(fill_key(f))
                out.append(f)
        return out


def build_positions(fills: list[dict]) -> list[dict]:
    """Split a wallet's fills into positions per token. A position opens on a BUY from flat; later
    buys are adds (not copied); each sell is kept as the fraction of the wallet's holding it sold.
    Sells of tokens bought before the window are ignored."""
    by_token = defaultdict(list)
    for f in sorted(fills, key=lambda f: (f["ts"], f["side"] == "SELL")):
        by_token[f["token"]].append(f)
    out = []
    for token, fs in by_token.items():
        pos, held = None, 0.0
        for f in fs:
            if f["side"] == "BUY":
                if pos is None:
                    pos = {"token": token, "cond": f["cond"], "title": f["title"], "outcome": f["outcome"],
                           "slug": f.get("slug", ""), "event_slug": f.get("event_slug", ""),
                           "open_ts": f["ts"], "cost": 0.0, "size": 0.0, "adds": 0, "sells": [], "close_ts": None,
                           "peak": 0.0}
                    out.append(pos)
                if f["ts"] == pos["open_ts"]:  # one order often fills against several makers
                    pos["cost"] += f["price"] * f["size"]
                    pos["size"] += f["size"]
                else:
                    pos["adds"] += 1
                held += f["size"]
                pos["peak"] = max(pos["peak"], held)
            elif pos is not None:
                before, held = held, held - f["size"]
                flat = held <= 0.01 * pos["peak"]
                pos["sells"].append((f["ts"], 1.0 if flat else f["size"] / before))
                if flat:
                    pos["close_ts"], pos, held = f["ts"], None, 0.0
    for p in out:
        p["fill"] = p["cost"] / p["size"] if p["size"] else 0.0
    return sorted(out, key=lambda p: p["open_ts"])


def category(raw: dict) -> str:
    if raw.get("sportsMarketType") or raw.get("gameStartTime") or str(raw.get("feeType") or "").startswith("sports"):
        return "sports"
    slugs = {str(t.get("slug") or "").lower() for t in raw.get("tags") or [] if isinstance(t, dict)}
    for name, keys in CATEGORY_TAGS:
        if slugs & keys:
            return name
    return str(raw.get("category") or "other").lower()


def fee_params(raw: dict, cat: str) -> tuple[float, float, str]:
    """(rate, exponent, source) of the market's taker fee, read from its own fields when it has them."""
    sched = raw.get("feeSchedule") if isinstance(raw.get("feeSchedule"), dict) else {}
    if raw.get("feesEnabled") is False or raw.get("feeType") == "zero_fees":
        return 0.0, 1.0, "fee-free"
    if num(sched.get("rate")) is not None:
        return num(sched["rate"]), num(sched.get("exponent")) or 1.0, "market"
    if raw.get("feesEnabled"):
        return CATEGORY_FEES.get(cat, 0.0), 1.0, "category default"
    return (SPORTS_FEE, 1.0, "assumed") if cat == "sports" else (0.0, 1.0, "assumed fee-free")


def fee_per_share(price: float, rate: float, exponent: float = 1.0) -> float:
    """Polymarket's taker fee per share: rate * (p * (1 - p)) ** exponent. Makers pay nothing."""
    return rate * (price * (1 - price)) ** exponent if 0 < price < 1 else 0.0


def norm_market(raw: dict) -> dict:
    """What the watch needs from a Gamma market: per-token payout, close state, fees, category, and
    enough of the game (teams, start, line) to look for the same bet on Kalshi."""
    tokens = [str(t) for t in jlist(raw.get("clobTokenIds"))]
    prices = [num(p) for p in jlist(raw.get("outcomePrices"))]
    closed = bool(raw.get("closed"))
    resolved = closed and bool(tokens) and len(prices) == len(tokens) and all(
        p is not None and min(abs(p), abs(p - 0.5), abs(p - 1)) < 1e-6 for p in prices)
    cat = category(raw)
    rate, exponent, source = fee_params(raw, cat)
    events = [e for e in raw.get("events") or [] if isinstance(e, dict)]
    start = first(raw, "gameStartTime", "eventStartTime") or (events and first(events[0], "startTime")) or None
    try:
        start_ts = int(parse_time(str(start)).timestamp()) if start else None
    except ValueError:
        start_ts = None
    return {"question": str(raw.get("question") or ""), "slug": str(raw.get("slug") or ""),
            "event_title": str((events and events[0].get("title")) or ""),
            "event_slug": str((events and events[0].get("slug")) or ""),
            "outcomes": [str(o) for o in jlist(raw.get("outcomes"))], "tokens": tokens,
            "price": dict(zip(tokens, prices)), "closed": closed, "resolved": resolved,
            "accepting": raw.get("acceptingOrders") is not False,
            "close_ts": epoch(first(raw, "closedTime", "umaEndDate", "endDate")) if closed else None,
            "game_start": start_ts, "line": num(raw.get("line")),
            "category": cat, "type": str(raw.get("sportsMarketType") or ""), "fee_rate": rate, "fee_exp": exponent,
            "fee_source": source}


def markets_for(http: Http, conds: list[str]) -> dict:
    """Gamma markets by condition id. Some deployments hide closed markets unless asked, so ids
    missing from the first answer are asked again with closed=true."""
    out = {}
    for i in range(0, len(conds), GAMMA_BATCH):
        batch = conds[i:i + GAMMA_BATCH]
        for extra in ({}, {"closed": "true"}):
            missing = [c for c in batch if c not in out]
            if not missing:
                break
            try:
                body = http.get(f"{GAMMA}/markets", {"condition_ids": missing, "limit": len(missing),
                                                     "include_tag": "true", **extra})
            except Exception as e:
                print(f"gamma: {str(e)[:200]}", flush=True)
                continue
            items = body if isinstance(body, list) else (body or {}).get("markets") or (body or {}).get("data") or []
            for raw in items:
                cid = str(first(raw, "conditionId", "condition_id") or "").lower()
                if cid in missing:
                    try:
                        out[cid] = norm_market(raw)
                    except Exception as e:
                        print(f"gamma {cid[:12]}: {e}", flush=True)
    return out


def book(http: Http, token: str) -> dict | None:
    """The CLOB book for one outcome token as {"bids": [(price, size)] best first, "asks": [...]},
    or None when the market has no book (closed, or never listed on the CLOB)."""
    try:
        body = http.get(f"{CLOB}/book", {"token_id": token})
    except Exception as e:
        print(f"book {token[:12]}: {str(e)[:160]}", flush=True)
        return None
    if not isinstance(body, dict) or "bids" not in body and "asks" not in body:
        return None

    def levels(side):
        out = [(num(x.get("price")), num(x.get("size"))) for x in body.get(side) or [] if isinstance(x, dict)]
        return [(p, s) for p, s in out if p is not None and s and 0 < p < 1]
    return {"bids": sorted(levels("bids"), reverse=True), "asks": sorted(levels("asks"))}


def walk_buy(asks: list[tuple[float, float]], dollars: float, rate: float, exponent: float) -> dict:
    """Spend up to `dollars` (fee included) up the asks, best first."""
    shares = spent = fees = 0.0
    for price, size in asks:
        each = price + fee_per_share(price, rate, exponent)
        take = min(size, (dollars - spent) / each)
        if take <= 0:
            break
        shares += take
        spent += take * each
        fees += take * fee_per_share(price, rate, exponent)
        if spent >= dollars - 1e-9:
            break
    return {"shares": shares, "spent": spent, "fees": fees,
            "avg": (spent - fees) / shares if shares else None}


def walk_sell(bids: list[tuple[float, float]], shares: float, rate: float, exponent: float) -> dict:
    """Sell up to `shares` down the bids, best first; cash is net of the taker fee."""
    left, cash = shares, 0.0
    for price, size in bids:
        take = min(size, left)
        if take <= 0:
            break
        cash += take * (price - fee_per_share(price, rate, exponent))
        left -= take
    return {"sold": shares - left, "cash": cash}
