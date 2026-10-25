import datetime as dt

from maker_sim import sim
from polymarket_watch import poly

UTC = dt.timezone.utc
T0 = int(dt.datetime(2026, 10, 22, 15, 0, tzinfo=UTC).timestamp())
SERIES = {"series": [{"ticker": "KXHIGHNY", "title": "Highest temperature in NYC today?", "frequency": "daily"}]}


def mk(ticker, bid, ask, day="26OCT22"):
    return {"ticker": f"KXHIGHNY-{day}-{ticker}", "event_ticker": f"KXHIGHNY-{day}",
            "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}"}


class FakeHttp(poly.Http):
    def __init__(self):
        super().__init__(min_interval=0, sleep=lambda s: None)
        self.markets, self.trades, self.results = [], {}, {}

    def get(self, url, params=None):
        if url.endswith("/series"):
            return SERIES
        if url.endswith("/markets/trades"):
            rows = [t for t in self.trades.get(params["ticker"], [])
                    if params["min_ts"] <= dt.datetime.fromisoformat(t["created_time"]).timestamp() <= params["max_ts"]]
            return {"trades": rows, "cursor": ""}
        if url.endswith("/markets"):
            return {"markets": self.markets, "cursor": ""}
        if "/markets/" in url:
            return {"market": {"result": self.results.get(url.rsplit("/", 1)[1], "")}}
        raise AssertionError(url)


def trade(ts, price, qty, taker, i):
    return {"trade_id": f"t{i}", "yes_price_dollars": f"{price:.4f}", "count_fp": f"{qty:.2f}", "taker_side": taker,
            "created_time": dt.datetime.fromtimestamp(ts, UTC).isoformat()}


def test_quotes_inside_the_spread_only_when_wide_enough_and_within_inventory():
    assert sim.quote_for(mk("B1", 0.40, 0.46), 0) | {} == {"bid": 0.41, "ask": 0.45, "mid": 0.43, "left_bid": 10,
                                                           "left_ask": 10}
    assert sim.quote_for(mk("B1", 0.40, 0.42), 0) is None          # 2c gap: nothing to stand inside
    assert sim.quote_for(mk("B1", 0.01, 0.06), 0) is None          # bid under 3c
    assert sim.quote_for(mk("B1", 0.40, 0.46), 45)["bid"] is None  # long 45: no more buying
    assert sim.quote_for(mk("B1", 0.40, 0.46), -45)["ask"] is None
    assert sim.day_of("KXHIGHNY-26OCT22") == "2026-10-22" and sim.maker_fee(0.5, 10) == 0.05


def test_a_round_trip_fills_both_sides_marks_out_and_settles(tmp_path):
    http = FakeHttp()
    http.markets = [mk("B1", 0.40, 0.46)]
    sim.tick(tmp_path, http, T0)                                    # quotes 41 / 45
    tk = "KXHIGHNY-26OCT22-B1"
    http.trades[tk] = [trade(T0 + 60, 0.40, 25, "no", 1),           # seller hits the bid: we buy 10 at 41
                       trade(T0 + 120, 0.46, 4, "yes", 2),          # buyer lifts the ask: we sell 4 at 45
                       trade(T0 + 130, 0.43, 50, "yes", 3),         # under our ask: no fill
                       trade(T0 + 140, 0.46, 0.5, "yes", 4)]        # half a contract: no whole fill
    res = sim.tick(tmp_path, http, T0 + 300)
    assert res["fills"] == 2
    _, fills, pos, _ = sim.load(tmp_path)
    assert [(f["side"], f["price"], f["qty"]) for f in fills] == [("buy", "0.41", "10"), ("sell", "0.45", "4")]
    p = pos[tk]
    assert p["inv"] == 6 and abs(p["cash"] - (-4.10 - 0.05 + 1.80 - 0.02)) < 1e-9
    http.markets = [mk("B1", 0.30, 0.34)]                           # price falls an hour later
    sim.tick(tmp_path, http, T0 + 3700)
    _, fills, _, _ = sim.load(tmp_path)
    assert fills[0]["mid_60m"] == "0.32"
    http.markets, http.results = [], {tk: "no"}                     # the market resolves NO
    sim.tick(tmp_path, http, T0 + 90000)
    _, _, pos, _ = sim.load(tmp_path)
    assert pos[tk]["settled"] and pos[tk]["held"] == 6 and pos[tk]["inv"] == 0
    summary = (tmp_path / "summary.md").read_text()
    assert "| 2 | 14 |" in summary and "Settled P&L so far: $-2.37" in summary
