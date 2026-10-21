import csv
import datetime as dt

from polymarket_watch import poly
from weather_watch import watch as W

ET = W.ET


def at(day: int, hour: int, minute: int = 0) -> int:
    return int(dt.datetime(2026, 10, day, hour, minute, tzinfo=ET).timestamp())


SERIES = {"series": [
    {"ticker": "KXHIGHNY", "title": "Highest temperature in NYC today?", "frequency": "daily"},
    {"ticker": "KXHIGHCHI", "title": "Chicago Daily Max Temp", "frequency": "daily"},
    {"ticker": "KXHIGHTEMPYEAR", "title": "Hottest year", "frequency": "annual"},
    {"ticker": "KXRAINNY", "title": "Rain in NYC", "frequency": "daily"},
]}


def bracket(ticker, bid, ask, day="26OCT22", no_ask=None, sub="75° to 76°"):
    m = {"ticker": f"{ticker}-{day}-B{bid}", "event_ticker": f"{ticker}-{day}", "yes_sub_title": sub,
         "yes_bid_dollars": f"{bid:.4f}", "yes_ask_dollars": f"{ask:.4f}", "volume_fp": "500.00"}
    if no_ask is not None:
        m["no_ask_dollars"] = f"{no_ask:.4f}"
    return m


class FakeHttp(poly.Http):
    def __init__(self, markets=None, results=None):
        super().__init__(min_interval=0, sleep=lambda s: None)
        self.markets, self.results, self.calls = markets or {}, results or {}, []

    def get(self, url, params=None):
        self.calls.append((url, params))
        if url.endswith("/series"):
            return SERIES
        if url.endswith("/markets"):
            return {"markets": self.markets.get(params["series_ticker"], []), "cursor": ""}
        ticker = url.rsplit("/", 1)[1]
        return {"market": {"ticker": ticker, "result": self.results.get(ticker, "")}}


MARKETS = {
    "KXHIGHNY": [bracket("KXHIGHNY", 0.02, 0.03, no_ask=0.98),   # sold
                 bracket("KXHIGHNY", 0.04, 0.10),                 # spread too wide
                 bracket("KXHIGHNY", 0.05, 0.06),                 # above the band
                 bracket("KXHIGHNY", 0.01, 0.02, day="26OCT23")],  # tomorrow's market
    "KXHIGHCHI": [bracket("KXHIGHCHI", 0.01, 0.02),               # sold, NO ask from the bid
                  {"ticker": "KXHIGHCHI-26OCT22-B9", "event_ticker": "KXHIGHCHI-26OCT22", "yes_bid_dollars": "0.02"}],
}


def test_fee_rounds_up_per_order():
    assert W.order_fee(0.98) == 0.14  # 7 x 0.98 x 0.02 = 0.1372 -> 14c
    assert W.order_fee(0.50) == 1.75


def test_discovery_keeps_daily_highs_only():
    found = W.discover(FakeHttp())
    assert set(found) == {"KXHIGHNY", "KXHIGHCHI"}
    assert found["KXHIGHCHI"] == "Chicago"


def test_sells_only_in_band_tight_brackets_for_today(tmp_path):
    res = W.tick(tmp_path, FakeHttp(MARKETS), at(22, 17, 5))
    got = {t["ticker"]: t for t in res["opened"]}
    assert set(got) == {"KXHIGHNY-26OCT22-B0.02", "KXHIGHCHI-26OCT22-B0.01"}
    ny, chi = got["KXHIGHNY-26OCT22-B0.02"], got["KXHIGHCHI-26OCT22-B0.01"]
    assert ny["no_ask"] == 0.98 and ny["cost"] == 98.14
    assert chi["no_ask"] == 0.99 and chi["fee"] == 0.07


def test_scans_once_a_day_and_never_outside_the_window(tmp_path):
    http = FakeHttp(MARKETS)
    assert W.tick(tmp_path, http, at(22, 16, 55))["opened"] == []
    assert len(W.tick(tmp_path, http, at(22, 17, 0))["opened"]) == 2
    assert W.tick(tmp_path, http, at(22, 17, 10))["opened"] == []  # already scanned today


def test_a_missed_window_is_logged_not_made_up(tmp_path):
    res = W.tick(tmp_path, FakeHttp(MARKETS), at(22, 18, 0))
    assert res["opened"] == [] and "missed" in res["notes"][0]
    assert W.tick(tmp_path, FakeHttp(MARKETS), at(22, 17, 20))["opened"] == []  # the day is closed


def test_settles_wins_and_losses_and_reports(tmp_path):
    W.tick(tmp_path, FakeHttp(MARKETS), at(22, 17, 5))
    results = {"KXHIGHNY-26OCT22-B0.02": "no", "KXHIGHCHI-26OCT22-B0.01": "yes"}
    assert W.tick(tmp_path, FakeHttp({}, results), at(22, 23))["settled"] == 0  # not until the day is over
    assert W.tick(tmp_path, FakeHttp({}, results), at(23, 9))["settled"] == 2
    with (tmp_path / "trades.csv").open() as f:
        t = {r["ticker"]: r for r in csv.DictReader(f)}
    assert float(t["KXHIGHNY-26OCT22-B0.02"]["pnl"]) == 1.86    # 100 - 98.14
    assert float(t["KXHIGHCHI-26OCT22-B0.01"]["pnl"]) == -99.07  # lost the 99c paid and the fee
    summary = (tmp_path / "summary.md").read_text()
    assert "Running: 2 of 1000" in summary and "## Losses" in summary and "Chicago" in summary


def test_interval_is_clustered_by_city_day():
    same_day = [{"city": "A", "day": "d1", "pnl_c": c} for c in (1, 1, 1, -99)]
    split = [{"city": c, "day": d, "pnl_c": p} for c, d, p in
             [("A", "d1", 1), ("B", "d1", 1), ("A", "d2", 1), ("B", "d2", -99)]]
    assert W.clustered(same_day)["lo"] is None  # one city-day: no interval
    s = W.clustered(split)
    assert s["groups"] == 4 and s["lo"] < s["mean"] < s["hi"]


def test_verdict_waits_for_the_bar():
    s = {"n": 10, "mean": 1.0, "lo": 0.5, "hi": 1.5}
    assert W.verdict(s, dt.date(2026, 11, 1)).startswith("Running")
    assert "Passed" in W.verdict(s | {"n": 1000}, dt.date(2026, 11, 1))
    assert "Failed" in W.verdict(s | {"lo": -0.2}, dt.date(2027, 2, 1))


def test_city_names_are_cleaned_so_one_city_clusters_once():
    assert W.clean_city("Daily High Temperature Houston") == "Houston"
    assert W.clean_city("Houston") == "Houston"
    assert W.clean_city("Oklahoma City Maximum High Temperature") == "Oklahoma City"
    assert W.clean_city("HIGHEST Temperature SATX") == "San Antonio"
    assert W.clean_city("Highest temperature in NYC today?") == "New York"
    assert W.clean_city("Daly high temp Sao Paulo") == "Sao Paulo"
    assert W.clean_city("Newark, NJ (EWR)") == "Newark, NJ (EWR)"


def test_old_trades_are_regrouped_by_clean_city(tmp_path):
    W.save(tmp_path, {"A": {"ticker": "A", "series": "KXHIGHOU", "city": "Houston", "day": "2026-10-22", "status": "open"},
                      "B": {"ticker": "B", "series": "KXHIGHTHOU", "city": "Daily High Temperature Houston",
                            "day": "2026-10-22", "status": "open"}}, {})
    trades, _ = W.load(tmp_path)
    assert {t["city"] for t in trades.values()} == {"Houston"}
