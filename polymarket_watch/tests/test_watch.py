import csv
import datetime as dt
import json

from polymarket_watch import kalshi, poly, watch

NOW = int(dt.datetime(2026, 10, 6, 20, 0, tzinfo=dt.timezone.utc).timestamp())
A = "0xaaaa000000000000000000000000000000000001"
COND, COND2 = "0x" + "c1" * 32, "0x" + "c2" * 32
WALLETS = [{"wallet": A, "name": "Elaran1993", "backtest_roi_60m": 0.11}]


def fill(ts, side, price, size, token="T1", cond=COND, title="Will Deportivo Alavés win on 2026-10-06?",
         outcome="Yes", tx=None):
    return {"proxy_wallet": A, "timestamp": ts, "condition_id": cond, "type": "TRADE", "size": size, "price": price,
            "token_id": token, "side": side, "transaction_hash": tx or f"0x{ts}{side}", "title": title,
            "outcome": outcome, "slug": "lal-ala-val-2026-10-06-ala", "event_slug": "lal-ala-val-2026-10-06"}


def gamma(cond=COND, tokens=("T1", "T2"), closed=False, prices=("0.45", "0.55"), question=None, kind="moneyline",
          start="2026-10-06 19:00:00+00"):
    return {"conditionId": cond, "question": question or "Will Deportivo Alavés win on 2026-10-06?",
            "slug": "lal-ala-val-2026-10-06-ala", "outcomes": '["Yes", "No"]', "clobTokenIds": json.dumps(list(tokens)),
            "outcomePrices": json.dumps(list(prices)), "closed": closed, "acceptingOrders": not closed,
            "closedTime": "2026-10-06 23:00:00+00" if closed else None, "sportsMarketType": kind,
            "gameStartTime": start, "feesEnabled": True,
            "feeSchedule": {"rate": 0.05, "exponent": 1}, "tags": [{"slug": "soccer"}],
            "events": [{"title": "Deportivo Alavés vs. Valencia CF", "slug": "lal-ala-val-2026-10-06"}]}


KALSHI_GAME = [
    {"ticker": "KXLALIGAGAME-26OCT06ALAVAL-ALA", "event_ticker": "KXLALIGAGAME-26OCT06ALAVAL",
     "yes_sub_title": "Alaves", "yes_bid_dollars": "0.44", "yes_ask_dollars": "0.46"},
    {"ticker": "KXLALIGAGAME-26OCT06ALAVAL-TIE", "event_ticker": "KXLALIGAGAME-26OCT06ALAVAL",
     "yes_sub_title": "Tie", "yes_bid_dollars": "0.27", "yes_ask_dollars": "0.29"},
    {"ticker": "KXLALIGAGAME-26OCT06ALAVAL-VAL", "event_ticker": "KXLALIGAGAME-26OCT06ALAVAL",
     "yes_sub_title": "Valencia", "yes_bid_dollars": "0.25", "yes_ask_dollars": "0.27"},
    {"ticker": "KXLALIGAGAME-26OCT07BETSEV-BET", "event_ticker": "KXLALIGAGAME-26OCT07BETSEV",
     "yes_sub_title": "Real Betis", "yes_bid_dollars": "0.50", "yes_ask_dollars": "0.52"},
]


class FakeHttp(poly.Http):
    def __init__(self, fills=(), markets=(), books=None, kalshi_series=None, kalshi_results=None):
        super().__init__(min_interval=0, sleep=lambda s: None)
        self.fills, self.markets, self.books = list(fills), list(markets), books or {}
        self.kalshi_series, self.kalshi_results = kalshi_series or {}, kalshi_results or {}
        self.calls, self.v1_gone = [], False

    def get(self, url, params=None):
        self.count += 1
        self.calls.append((url, params))
        if url.endswith("/activity"):
            rows = sorted((f for f in self.fills if params["start"] <= f["timestamp"] <= params["end"]),
                          key=lambda f: -f["timestamp"])
            if url.endswith("/v2/activity"):
                return {"data": rows, "pagination": {}}
            if self.v1_gone:
                raise RuntimeError("HTTP 410")
            return rows[params["offset"]:params["offset"] + params["limit"]]
        if url.endswith("/markets") and "gamma" in url:
            return [m for m in self.markets if m["conditionId"] in params["condition_ids"]]
        if url.endswith("/book"):
            return self.books.get(params["token_id"], {"bids": [], "asks": []})
        if "kalshi" in url and url.endswith("/markets"):
            return {"markets": self.kalshi_series.get(params["series_ticker"], []), "cursor": None}
        if "kalshi" in url and "/markets/" in url:
            return {"market": {"result": self.kalshi_results.get(url.rsplit("/", 1)[1], "")}}
        raise AssertionError(url)


def book(bid, ask, size=1000):
    return {"bids": [{"price": str(bid), "size": str(size)}], "asks": [{"price": str(ask), "size": str(size)}]}


def rows(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def test_first_tick_copies_only_new_openings_and_matches_kalshi(tmp_path):
    fills = [fill(NOW - 2 * 86400, "BUY", 0.30, 100, token="OLD", cond=COND2),   # history: opened before the start
             fill(NOW - 600, "BUY", 0.35, 50, token="OLD", cond=COND2),          # an add, not an opening
             fill(NOW - 20 * 60, "BUY", 0.44, 500),                              # a new opening: copied
             fill(NOW - 20 * 60, "BUY", 0.45, 300, tx="0xsplit")]                # same order, second maker
    http = FakeHttp(fills, [gamma(), gamma(COND2, ("OLD", "OLD2"))], {"T1": book(0.44, 0.46)},
                    {"KXLALIGAGAME": KALSHI_GAME})
    res = watch.tick(tmp_path, http, NOW, WALLETS)
    copies = rows(tmp_path / "copies.csv")
    assert len(copies) == 1 and copies[0]["status"] == "copied"
    c = copies[0]
    assert c["wallet_fill"] == str(round((0.44 * 500 + 0.45 * 300) / 800, 4)) and c["delay_min"] == "20.0"
    fee = 0.05 * 0.46 * 0.54
    assert abs(float(c["shares"]) - 100 / (0.46 + fee)) < 1e-3 and abs(float(c["spent"]) - 100) < 1e-6
    assert c["kalshi_ticker"] == "KXLALIGAGAME-26OCT06ALAVAL-ALA" and c["kalshi_side"] == "yes"
    assert c["kalshi_ask"] == "0.46"
    assert [a["id"] for a in res["alerts"]] == [c["id"]]
    text = "\n".join(watch.alert_text(res["alerts"], watch.load_copies(tmp_path)))
    assert "Elaran1993 bought Yes at 44c" in text and "Kalshi: YES on 'Alaves' at 46c" in text
    assert "PAPER" in text
    unmatched = dict(res["alerts"][0], kalshi_ticker="", title="Exact Score: Alavés 2 - 1 Valencia?")
    text = "\n".join(watch.alert_text([unmatched], {}))
    assert "no Kalshi twin:\n- Exact Score: Alavés 2 - 1 Valencia?: Yes (Elaran1993, Polymarket 46c)" in text
    meta = json.loads((tmp_path / "watch.json").read_text())
    assert meta["started_ts"] == NOW - 3600 and meta["wallets"][A]["last_ts"] == NOW - 600


def test_sells_are_mirrored_once_and_copies_settle(tmp_path):
    fills = [fill(NOW - 20 * 60, "BUY", 0.44, 500)]
    http = FakeHttp(fills, [gamma()], {"T1": book(0.44, 0.46)}, {"KXLALIGAGAME": KALSHI_GAME})
    watch.tick(tmp_path, http, NOW, WALLETS)
    shares = float(rows(tmp_path / "copies.csv")[0]["shares"])

    # The wallet sells half; the copy sells half of its own shares at the live bid.
    http.fills.append(fill(NOW + 300, "SELL", 0.60, 250))
    http.books["T1"] = book(0.60, 0.62)
    watch.tick(tmp_path, http, NOW + 600, WALLETS)
    watch.tick(tmp_path, http, NOW + 1200, WALLETS)  # the same sell is not mirrored again
    c = rows(tmp_path / "copies.csv")[0]
    assert abs(float(c["sold_shares"]) - shares / 2) < 1e-3 and c["sells_mirrored"] == "1"
    cash = shares / 2 * (0.60 - 0.05 * 0.6 * 0.4)
    assert abs(float(c["sale_cash"]) - cash) < 1e-2 and c["exit"] == "part sold"

    # Alavés wins: the rest pays $1 a share. Kalshi settles the same bet.
    http.markets = [gamma(closed=True, prices=("1", "0"))]
    http.kalshi_results = {"KXLALIGAGAME-26OCT06ALAVAL-ALA": "yes"}
    watch.tick(tmp_path, http, NOW + 4 * 3600, WALLETS)
    c = rows(tmp_path / "copies.csv")[0]
    assert c["exit"] == "part sold+resolved" and c["settled"]
    assert abs(float(c["pnl"]) - (cash + shares / 2 - 100)) < 1e-2
    assert c["kalshi_result"] == "win"
    assert abs(float(c["kalshi_roi"]) - (1 - 0.46 - 0.07 * 0.46 * 0.54) / (0.46 + 0.07 * 0.46 * 0.54)) < 1e-3
    summary = (tmp_path / "summary.md").read_text()
    assert "| Sports (judged) | 1 | 1-0 |" in summary and "Running: 1 of 300" in summary


def test_chases_small_buys_and_late_fills(tmp_path):
    fills = [fill(NOW - 600, "BUY", 0.40, 100),                                   # price ran to 55c: chase
             fill(NOW - 500, "BUY", 0.50, 4, token="T9", cond=COND2)]             # $2: too small
    http = FakeHttp(fills, [gamma(), gamma(COND2, ("T9", "T10"))], {"T1": book(0.54, 0.56)})
    res = watch.tick(tmp_path, http, NOW, WALLETS)
    status = {c["token"]: c["status"] for c in rows(tmp_path / "copies.csv")}
    assert status == {"T1": "skipped_chase", "T9": "too_small"} and not res["alerts"]

    # A fill indexed late, earlier than the copied opening, moves the position's start but is the same position.
    tmp = tmp_path / "late"
    http = FakeHttp([fill(NOW - 600, "BUY", 0.44, 500)], [gamma()], {"T1": book(0.44, 0.46)})
    watch.tick(tmp, http, NOW, WALLETS)
    http.fills.append(fill(NOW - 900, "BUY", 0.43, 100))
    watch.tick(tmp, http, NOW + 600, WALLETS)
    assert len(rows(tmp / "copies.csv")) == 1


def test_kalshi_matching_by_bet_type():
    spread_events = [
        {"ticker": "KXNFLGAME-26OCT12DETNYJ-DET", "event_ticker": "KXNFLGAME-26OCT12DETNYJ",
         "yes_sub_title": "Detroit", "yes_bid_dollars": "0.70", "yes_ask_dollars": "0.71"},
        {"ticker": "KXNFLGAME-26OCT12DETNYJ-NYJ", "event_ticker": "KXNFLGAME-26OCT12DETNYJ",
         "yes_sub_title": "New York J", "yes_bid_dollars": "0.29", "yes_ask_dollars": "0.30"}]
    ladder = [{"ticker": f"KXNFLSPREAD-26OCT12DETNYJ-DET{n}", "event_ticker": "KXNFLSPREAD-26OCT12DETNYJ",
               "yes_sub_title": f"Detroit wins by over {n - 0.5} Points", "floor_strike": n - 0.5,
               "yes_bid_dollars": "0.48", "yes_ask_dollars": "0.50"} for n in (4, 7, 10)]
    totals = [{"ticker": "KXNFLTOTAL-26OCT12DETNYJ-45", "event_ticker": "KXNFLTOTAL-26OCT12DETNYJ",
               "yes_sub_title": "Over 44.5 points scored", "floor_strike": 44.5, "yes_bid_dollars": "0.51",
               "yes_ask_dollars": "0.53"}]
    k = kalshi.Kalshi(FakeHttp(kalshi_series={"KXNFLGAME": spread_events, "KXNFLSPREAD": ladder,
                                               "KXNFLTOTAL": totals, "KXLALIGAGAME": KALSHI_GAME}))
    base = {"category": "sports", "event_title": "Lions vs. Jets", "outcomes": ["Lions", "Jets"]}
    ev = "nfl-det-nyj-2026-10-12"

    dog = kalshi.match(k, base | {"question": "Spread: Lions (-6.5)", "type": "spreads"}, "Jets", ev, ev)
    assert dog["ticker"] == "KXNFLSPREAD-26OCT12DETNYJ-DET7" and dog["side"] == "no" and dog["ask"] == 0.52
    fav = kalshi.match(k, base | {"question": "Spread: Lions (-6.5)", "type": "spreads"}, "Lions", ev, ev)
    assert fav["side"] == "yes" and fav["ask"] == 0.50
    off = kalshi.match(k, base | {"question": "Spread: Lions (-5.5)", "type": "spreads"}, "Lions", ev, ev)
    assert "no 5.5 line" in off["note"] and "3.5, 6.5, 9.5" in off["note"]
    ml = kalshi.match(k, base | {"question": "Lions vs. Jets", "type": "moneyline"}, "Jets", ev, ev)
    assert ml["ticker"] == "KXNFLGAME-26OCT12DETNYJ-NYJ" and ml["side"] == "yes"
    under = kalshi.match(k, base | {"question": "Lions vs. Jets: O/U 44.5", "type": "totals",
                                    "outcomes": ["Over", "Under"]}, "Under", ev, ev)
    assert under["ticker"] == "KXNFLTOTAL-26OCT12DETNYJ-45" and under["side"] == "no" and under["ask"] == 0.49
    half = kalshi.match(k, base | {"question": "Lions vs. Jets: 1H Moneyline", "type": "first_half_moneyline"},
                        "Lions", ev, ev)
    assert "not mapped" in half["note"]

    soccer = {"category": "sports", "event_title": "Deportivo Alavés vs. Valencia CF", "outcomes": ["Yes", "No"],
              "type": "moneyline"}
    sev = "lal-ala-val-2026-10-06"
    no_val = kalshi.match(k, soccer | {"question": "Will Valencia CF win on 2026-10-06?"}, "No", sev, sev + "-val")
    assert no_val["ticker"] == "KXLALIGAGAME-26OCT06ALAVAL-VAL" and no_val["side"] == "no" and no_val["ask"] == 0.75
    draw = kalshi.match(k, soccer | {"question": "Will Deportivo Alavés vs. Valencia CF end in a draw?"}, "Yes",
                        sev, sev + "-draw")
    assert draw["ticker"] == "KXLALIGAGAME-26OCT06ALAVAL-TIE" and draw["side"] == "yes"
    nowhere = kalshi.match(k, soccer | {"question": "Will Sevilla FC win on 2026-10-06?",
                                        "event_title": "Sevilla FC vs. Girona FC"}, "Yes",
                           "lal-sev-gir-2026-10-06", "lal-sev-gir-2026-10-06-sev")
    assert nowhere["note"] == "no matching Kalshi game"


def test_names_and_dates():
    assert kalshi.date_code(dt.date(2026, 10, 6)) == "26OCT06"
    assert kalshi.slug_parts("lal-ala-val-2026-09-15") == ("lal", ["ala", "val"], dt.date(2026, 9, 15))
    assert kalshi.slug_parts("unl-eng-cze-2026-10-06-more-markets") == ("unl", ["eng", "cze"], dt.date(2026, 10, 6))
    assert kalshi.name_score("Alaves", "Deportivo Alavés") > 0
    assert kalshi.name_score("Man City", "Manchester City FC") > kalshi.name_score("Man City", "Manchester United FC")
    assert kalshi.name_score("Real Madrid", "Real Sociedad de Fútbol") <= 0
    assert kalshi.split_teams("Will there be a run scored in the first inning?: St. Louis Cardinals vs. San "
                              "Francisco Giants") == ["St. Louis Cardinals", "San Francisco Giants"]
    assert kalshi.split_teams("Getafe CF vs. RC Celta de Vigo: O/U 2.5") == ["Getafe CF", "RC Celta de Vigo"]


def test_book_walks_and_fees():
    asks = [(0.40, 100), (0.42, 1000)]
    got = poly.walk_buy(asks, 100, 0.05, 1)
    first = 100 * (0.40 + 0.05 * 0.4 * 0.6)
    assert abs(got["spent"] - 100) < 1e-9 and got["shares"] > 100
    assert abs(got["shares"] - (100 + (100 - first) / (0.42 + 0.05 * 0.42 * 0.58))) < 1e-9
    sale = poly.walk_sell([(0.60, 10), (0.55, 100)], 20, 0.05, 1)
    assert sale["sold"] == 20 and abs(sale["cash"] - (10 * (0.6 - 0.012) + 10 * (0.55 - 0.05 * 0.55 * 0.45))) < 1e-9
    b = {"bids": [{"price": "0.40", "size": "5"}, {"price": "0.45", "size": "5"}],
         "asks": [{"price": "0.50", "size": "5"}, {"price": "0.47", "size": "5"}]}

    class BookHttp(FakeHttp):
        def get(self, url, params=None):
            return b
    got = poly.book(BookHttp(), "T1")
    assert got["bids"][0] == (0.45, 5.0) and got["asks"][0] == (0.47, 5.0)


def test_activity_falls_back_to_v2_when_v1_is_gone(tmp_path):
    http = FakeHttp([fill(NOW - 600, "BUY", 0.44, 500)], [gamma()], {"T1": book(0.44, 0.46)})
    http.v1_gone = True
    watch.tick(tmp_path, http, NOW, WALLETS)
    assert rows(tmp_path / "copies.csv")[0]["status"] == "copied"
    assert json.loads((tmp_path / "watch.json").read_text())["activity_api"] == "v2"


def test_closing_line_is_the_last_price_before_the_start(tmp_path):
    later = "2026-10-06 22:00:00+00"  # two hours after NOW
    http = FakeHttp([fill(NOW - 600, "BUY", 0.44, 500)], [gamma(start=later)], {"T1": book(0.44, 0.46)})
    watch.tick(tmp_path, http, NOW, WALLETS)
    http.markets = [gamma(start=later, prices=("0.52", "0.48"))]          # money came in behind the wallet
    watch.tick(tmp_path, http, NOW + 3600, WALLETS)
    http.markets = [gamma(start=later, prices=("0.60", "0.40"))]          # in play: not the closing line
    watch.tick(tmp_path, http, NOW + 3 * 3600, WALLETS)
    c = rows(tmp_path / "copies.csv")[0]
    assert c["close_mid"] == "0.52" and c["close_at"] == watch.iso(NOW + 3600)
    assert abs(float(c["clv_c"]) - (52 - float(c["avg_price"]) * 100)) < 1e-6 and float(c["wallet_clv_c"]) == 8.0
    summary = (tmp_path / "summary.md").read_text()
    assert "**Closing line**" in summary and "the wallets' own fills +8.0c" in summary


def test_near_certain_copies_are_recorded_but_not_announced(tmp_path, monkeypatch, capsys):
    http = FakeHttp([fill(NOW - 600, "BUY", 0.97, 500)], [gamma()], {"T1": book(0.97, 0.98)},
                    {"KXLALIGAGAME": KALSHI_GAME})
    sent = []
    import live_trader.src.notify as notify
    monkeypatch.setattr(notify, "send", lambda text: sent.append(text) or True)
    monkeypatch.setattr(watch.poly, "Http", lambda: http)
    monkeypatch.setattr(watch.time, "time", lambda: NOW)
    monkeypatch.setattr(watch, "load_wallets", lambda: WALLETS)
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    watch.main()
    assert rows(tmp_path / "copies.csv")[0]["status"] == "copied" and sent == []
    assert "1 copies at 95c or more recorded, not announced" in capsys.readouterr().out
